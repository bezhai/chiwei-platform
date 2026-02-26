/**
 * Generic state machine implementation
 *
 * Features:
 * - State transition validation
 * - Async callback management
 * - State stack tracking
 * - Error handling and state rollback
 */

export interface StateTransition<TState> {
    from: TState | TState[];
    to: TState;
    allowedFromAny?: boolean;
}

export interface StateHandler<TState, TData> {
    state: TState;
    handler: (data: TData, context: StateMachineContext<TState>) => Promise<void>;
    required?: boolean;
}

export interface StateMachineContext<TState> {
    currentState: TState | null;
    previousState: TState | null;
    stateStack: TState[];
    metadata: Record<string, unknown>;
}

export interface StateMachineOptions<TState> {
    initialState?: TState;
    transitions: StateTransition<TState>[];
    allowUnknownTransitions?: boolean;
    onInvalidTransition?: (from: TState | null, to: TState, error: string) => void;
    onStateChange?: (from: TState | null, to: TState, context: StateMachineContext<TState>) => void;
}

export class StateMachine<TState, TData = unknown> {
    private currentState: TState | null = null;
    private previousState: TState | null = null;
    private stateStack: TState[] = [];
    private metadata: Record<string, unknown> = {};

    private transitions: Map<string, TState[]> = new Map();
    private handlers: Map<TState, StateHandler<TState, TData>[]> = new Map();
    private pendingHandlers: Map<TState, Promise<void>[]> = new Map();

    private isTransitioning = false;
    private transitionQueue: Array<{
        to: TState;
        data?: TData;
        resolve: (success: boolean) => void;
        reject: (error: Error) => void;
    }> = [];

    private options: StateMachineOptions<TState>;

    constructor(options: StateMachineOptions<TState>) {
        this.options = options;
        this.currentState = options.initialState || null;

        for (const transition of options.transitions) {
            const fromStates = Array.isArray(transition.from) ? transition.from : [transition.from];
            for (const fromState of fromStates) {
                const key = String(fromState);
                if (!this.transitions.has(key)) {
                    this.transitions.set(key, []);
                }
                this.transitions.get(key)!.push(transition.to);
            }

            if (transition.allowedFromAny) {
                const anyKey = '*';
                if (!this.transitions.has(anyKey)) {
                    this.transitions.set(anyKey, []);
                }
                this.transitions.get(anyKey)!.push(transition.to);
            }
        }
    }

    on(
        state: TState,
        handler: (data: TData, context: StateMachineContext<TState>) => Promise<void>,
        required = false,
    ): this {
        if (!this.handlers.has(state)) {
            this.handlers.set(state, []);
        }
        this.handlers.get(state)!.push({ state, handler, required });
        return this;
    }

    private isValidTransition(from: TState | null, to: TState): boolean {
        if (this.options.allowUnknownTransitions) {
            return true;
        }

        const anyTransitions = this.transitions.get('*') || [];
        if (anyTransitions.includes(to)) {
            return true;
        }

        if (from === null) {
            return true;
        }

        const fromKey = String(from);
        const allowedTransitions = this.transitions.get(fromKey) || [];
        return allowedTransitions.includes(to);
    }

    async transition(to: TState, data?: TData): Promise<boolean> {
        return new Promise((resolve, reject) => {
            this.transitionQueue.push({ to, data, resolve, reject });

            if (!this.isTransitioning) {
                this.processTransitionQueue();
            }
        });
    }

    private async processTransitionQueue(): Promise<void> {
        if (this.isTransitioning || this.transitionQueue.length === 0) {
            return;
        }

        this.isTransitioning = true;

        while (this.transitionQueue.length > 0) {
            const { to, data, resolve, reject } = this.transitionQueue.shift()!;

            try {
                const success = await this.doTransition(to, data);
                resolve(success);
            } catch (error) {
                reject(error instanceof Error ? error : new Error(String(error)));
            }
        }

        this.isTransitioning = false;
    }

    private async doTransition(to: TState, data?: TData): Promise<boolean> {
        const from = this.currentState;

        if (!this.isValidTransition(from, to)) {
            const error = `Invalid transition from ${from} to ${to}`;
            this.options.onInvalidTransition?.(from, to, error);
            return false;
        }

        if (from !== null) {
            await this.waitForState(from);
        }

        this.previousState = this.currentState;
        this.currentState = to;
        this.stateStack.push(to);

        const context: StateMachineContext<TState> = {
            currentState: this.currentState,
            previousState: this.previousState,
            stateStack: [...this.stateStack],
            metadata: { ...this.metadata },
        };

        this.options.onStateChange?.(from, to, context);

        await this.executeHandlers(to, data, context);

        return true;
    }

    private async executeHandlers(
        state: TState,
        data: TData | undefined,
        context: StateMachineContext<TState>,
    ): Promise<void> {
        const handlers = this.handlers.get(state) || [];
        if (handlers.length === 0) {
            return;
        }

        const promises: Promise<void>[] = [];
        const requiredPromises: Promise<void>[] = [];

        for (const { handler, required } of handlers) {
            const promise = handler(data as TData, context).catch((error) => {
                console.error(`Handler for state ${state} failed:`, error);
                throw error;
            });

            promises.push(promise);
            if (required) {
                requiredPromises.push(promise);
            }
        }

        if (requiredPromises.length > 0) {
            await Promise.all(requiredPromises);
        }

        this.pendingHandlers.set(state, promises);
    }

    async waitForState(state: TState): Promise<void> {
        const promises = this.pendingHandlers.get(state) || [];
        if (promises.length > 0) {
            await Promise.all(promises);
            this.pendingHandlers.delete(state);
        }
    }

    async waitForAll(): Promise<void> {
        const allPromises = Array.from(this.pendingHandlers.values()).flat();
        if (allPromises.length > 0) {
            await Promise.all(allPromises);
            this.pendingHandlers.clear();
        }
    }

    getCurrentState(): TState | null {
        return this.currentState;
    }

    getStateStack(): TState[] {
        return [...this.stateStack];
    }

    getContext(): StateMachineContext<TState> {
        return {
            currentState: this.currentState,
            previousState: this.previousState,
            stateStack: [...this.stateStack],
            metadata: { ...this.metadata },
        };
    }

    setMetadata(key: string, value: unknown): void {
        this.metadata[key] = value;
    }

    getMetadata(key: string): unknown {
        return this.metadata[key];
    }

    isTransitionInProgress(): boolean {
        return this.isTransitioning;
    }

    getPendingTransitionCount(): number {
        return this.transitionQueue.length;
    }

    reset(): void {
        this.currentState = this.options.initialState || null;
        this.previousState = null;
        this.stateStack = [];
        this.metadata = {};
        this.pendingHandlers.clear();
        this.isTransitioning = false;
        this.transitionQueue = [];
    }
}
