import type { Context, Next } from 'koa';

/**
 * Validation error class
 */
export class ValidationError extends Error {
    constructor(message: string, public field: string) {
        super(message);
        this.name = 'ValidationError';
    }
}

/**
 * Validation rule type
 */
export interface ValidationRule {
    required?: boolean;
    type?: 'string' | 'number' | 'boolean';
    minLength?: number;
    maxLength?: number;
    pattern?: RegExp;
    custom?: (value: unknown) => boolean | string;
}

/**
 * Validation rules collection
 */
export interface ValidationRules {
    [key: string]: ValidationRule;
}

/**
 * Validate value type
 */
function validateType(value: unknown, type: string): boolean {
    switch (type) {
        case 'string':
            return typeof value === 'string';
        case 'number':
            return typeof value === 'number' && !isNaN(value);
        case 'boolean':
            return typeof value === 'boolean';
        default:
            return false;
    }
}

/**
 * Validate fields against rules
 */
function validateFields(data: Record<string, unknown>, rules: ValidationRules): void {
    for (const [fieldName, rule] of Object.entries(rules)) {
        const value = data[fieldName];

        // Check required field
        if (rule.required && (value === undefined || value === null || value === '')) {
            throw new ValidationError(`${fieldName} is required`, fieldName);
        }

        // Skip further validation if field is not present and not required
        if (value === undefined || value === null) {
            continue;
        }

        // Type validation
        if (rule.type && !validateType(value, rule.type)) {
            throw new ValidationError(`${fieldName} must be of type ${rule.type}`, fieldName);
        }

        // String length validation
        if (rule.type === 'string' || typeof value === 'string') {
            const strValue = value as string;
            if (rule.minLength && strValue.length < rule.minLength) {
                throw new ValidationError(
                    `${fieldName} must be at least ${rule.minLength} characters`,
                    fieldName
                );
            }
            if (rule.maxLength && strValue.length > rule.maxLength) {
                throw new ValidationError(
                    `${fieldName} must be at most ${rule.maxLength} characters`,
                    fieldName
                );
            }
        }

        // Pattern validation
        if (rule.pattern && typeof value === 'string' && !rule.pattern.test(value)) {
            throw new ValidationError(`${fieldName} format is invalid`, fieldName);
        }

        // Custom validation
        if (rule.custom) {
            const result = rule.custom(value);
            if (result !== true) {
                const message = typeof result === 'string' ? result : `${fieldName} validation failed`;
                throw new ValidationError(message, fieldName);
            }
        }
    }
}

/**
 * Create request body validation middleware
 */
export function validateBody(rules: ValidationRules) {
    return async (ctx: Context, next: Next) => {
        try {
            const body = (ctx.request as { body?: Record<string, unknown> }).body || {};
            validateFields(body, rules);
            await next();
        } catch (error) {
            if (error instanceof ValidationError) {
                ctx.status = 400;
                ctx.body = {
                    success: false,
                    message: `Validation failed: ${error.message}`,
                    field: error.field,
                    error_code: 'VALIDATION_ERROR',
                };
                return;
            }
            throw error;
        }
    };
}

/**
 * Create query parameter validation middleware
 */
export function validateQuery(rules: ValidationRules) {
    return async (ctx: Context, next: Next) => {
        try {
            const query = (ctx.request.query || {}) as Record<string, unknown>;
            validateFields(query, rules);
            await next();
        } catch (error) {
            if (error instanceof ValidationError) {
                ctx.status = 400;
                ctx.body = {
                    success: false,
                    message: `Query validation failed: ${error.message}`,
                    field: error.field,
                    error_code: 'VALIDATION_ERROR',
                };
                return;
            }
            throw error;
        }
    };
}
