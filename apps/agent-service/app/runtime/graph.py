"""compile_graph(): startup validation for the wired dataflow graph.

Walks ``WIRING_REGISTRY`` and verifies that:
  * every consumer referenced by a wire is decorated with ``@node``;
  * every ``.with_latest(X)`` has a matching ``wire(X).as_latest()`` declared
    somewhere else in the graph;
  * every consumer's signature actually accepts the data types the wire
    routes to it (the primary ``data_type`` plus any ``with_latest`` types).

Returns a ``CompiledGraph`` summarising the data types, nodes, and wires
seen. Errors surface as ``GraphError`` at startup so mis-wired graphs
never reach traffic.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.runtime.data import Data
from app.runtime.node import NODE_REGISTRY, inputs_of
from app.runtime.placement import DEFAULT_APP, nodes_for_app
from app.runtime.wire import WIRING_REGISTRY, WireSpec


class GraphError(Exception):
    pass


@dataclass
class CompiledGraph:
    data_types: set[type[Data]]
    nodes: set
    wires: list[WireSpec]


def compile_graph() -> CompiledGraph:
    wires = list(WIRING_REGISTRY)

    # 1) every consumer in wires must be @node-registered
    for w in wires:
        for c in w.consumers:
            if c not in NODE_REGISTRY:
                raise GraphError(
                    f"wire({w.data_type.__name__}).to({c.__name__}): consumer "
                    f"not registered as @node"
                )

    # 2) .with_latest(X) requires some wire(X).as_latest() to exist
    latest_types = {w.data_type for w in wires if w.as_latest}
    for w in wires:
        for t in w.with_latest:
            if t not in latest_types:
                raise GraphError(
                    f"wire({w.data_type.__name__}).with_latest({t.__name__}) "
                    f"requires wire({t.__name__}).as_latest() declared somewhere"
                )

    # 3) consumer signature must equal the wire's declared inputs
    # exactly. Subset-only matching ("consumer accepts at least these")
    # lets a consumer declare an extra Data param that no wire ever
    # populates — startup looks fine, then emit() raises a missing-kwarg
    # at first traffic. Strict equality also encodes the framework's
    # 1-consumer-1-wire design: if a function needs to react to two
    # different data types, write two @nodes (or one with ``Union[A, B]``
    # once that's modeled), don't reuse the same callable across wires.
    for w in wires:
        for c in w.consumers:
            ins = inputs_of(c)
            param_types = set(ins.values())
            needed = {w.data_type, *w.with_latest}
            if param_types != needed:
                extra = param_types - needed
                missing = needed - param_types
                hint = []
                if missing:
                    hint.append(
                        f"missing {sorted(t.__name__ for t in missing)}"
                    )
                if extra:
                    hint.append(
                        f"extra {sorted(t.__name__ for t in extra)} "
                        f"(declare on the wire via .with_latest(...) or "
                        f"split into a separate @node — a consumer must "
                        f"appear on exactly one wire)"
                    )
                raise GraphError(
                    f"wire({w.data_type.__name__}).to({c.__name__}): "
                    f"consumer signature {ins} does not match the wire "
                    f"inputs {sorted(t.__name__ for t in needed)} "
                    f"({'; '.join(hint)})"
                )

    # 3a) Source.mq wires must target exactly one single-input consumer.
    # The engine's MQ source loop reflects on the target @node to decide
    # how to decode a raw JSON body into a Data instance. Fan-out (2+
    # consumers) leaves the decode target ambiguous; multi-input targets
    # can't be populated from a single MQ frame. Reject both at compile
    # time so mis-wired graphs never reach start-up.
    for w in wires:
        mq_sources = [s for s in w.sources if s.kind == "mq"]
        if not mq_sources:
            continue
        if len(w.consumers) != 1:
            raise GraphError(
                f"wire({w.data_type.__name__}).from_(Source.mq(...)): MQ "
                f"source requires exactly one consumer; got "
                f"{len(w.consumers)} "
                f"({[c.__name__ for c in w.consumers]})"
            )
        (c,) = w.consumers
        ins = inputs_of(c)
        if len(ins) != 1:
            raise GraphError(
                f"wire({w.data_type.__name__}).from_(Source.mq(...)).to("
                f"{c.__name__}): MQ source target must take exactly one "
                f"Data arg; got signature {ins}"
            )

    # 4) placement consistency: a wire's consumers must all resolve to
    # the same app. Unbound consumers run in DEFAULT_APP at runtime
    # (``nodes_for_app`` treats ``NODE_REGISTRY - bound`` as belonging
    # to DEFAULT_APP), so the compile-time check has to mirror that —
    # otherwise "explicitly bound to agent-service + unbound" looks
    # mixed here while runtime sees them as the same app, and a wire
    # that's actually fine gets rejected at boot. Use the same default
    # so the validation matches dispatch semantics exactly.
    from app.runtime.placement import iter_bindings

    bindings = dict(iter_bindings())
    if bindings:
        for w in wires:
            apps = {bindings.get(c, DEFAULT_APP) for c in w.consumers}
            if len(apps) > 1:
                labels = sorted(
                    f"{c.__name__}->{bindings.get(c, DEFAULT_APP)}"
                    for c in w.consumers
                )
                raise GraphError(
                    f"wire({w.data_type.__name__}): consumers span mixed apps "
                    f"({', '.join(labels)}); split the wire or rebind "
                    f"consumers so they share one app"
                )

    # 4c) .debounce() canonical shape:
    #   * exactly one @node consumer (debounce state is keyed by
    #     DataType+key — fan-out would split the redis state across
    #     consumers in ways the engine can't reason about);
    #   * data type Meta.transient = True (fire signals are not
    #     persisted to pg);
    #   * key_by must be set (DSL enforces it; we re-check for
    #     defensive callers that build WireSpec directly);
    #   * cannot combine with ``.durable()`` (debounce ships its own
    #     mq transport via DELAYED_QUEUE), ``.as_latest()``
    #     (transient-only collides with insert_latest), ``.with_latest()``
    #     (debounce handlers are single-input), ``.when()`` (the
    #     ``DebounceReschedule`` reschedule path bypasses predicate
    #     evaluation), declarative ``Source.*`` (debounce wires are
    #     emit-driven), or ``Sink.*`` (the fire signal needs a
    #     business consumer, not a passthrough mq publish);
    #   * each (DataType) appears in at most one ``.debounce()`` wire
    #     (otherwise the redis ``debounce:latest:{DataType}:{key}``
    #     state would collide).
    # Runs ahead of the durable / transient blocks below so that
    # ``wire(...).debounce().durable()`` surfaces as the explicit
    # debounce-vs-durable error rather than the generic
    # durable-on-transient one.
    seen_debounce_types: set[type[Data]] = set()
    for w in wires:
        if w.debounce is None:
            continue
        if w.debounce_key_by is None:
            raise GraphError(
                f"wire({w.data_type.__name__}).debounce(...) requires "
                f"``key_by=`` (DSL enforces it; this WireSpec was likely "
                f"constructed directly without going through "
                f"``WireBuilder.debounce``)"
            )
        meta = getattr(w.data_type, "Meta", None)
        if meta is None or not getattr(meta, "transient", False):
            raise GraphError(
                f"wire({w.data_type.__name__}).debounce(): data type "
                f"must declare ``Meta.transient = True`` (debounce fire "
                f"signals are not persisted to pg)."
            )
        if w.durable:
            raise GraphError(
                f"wire({w.data_type.__name__}).debounce().durable(): "
                f"debounce already ships its own mq transport (delayed "
                f"queue + reschedule); combining with ``.durable()`` is "
                f"not supported. Drop ``.durable()``."
            )
        if w.as_latest:
            raise GraphError(
                f"wire({w.data_type.__name__}).debounce().as_latest(): "
                f"as_latest persists data via insert_latest, but debounce "
                f"data types must be ``Meta.transient = True`` (no pg "
                f"table) — these two are mutually exclusive. Drop one."
            )
        if w.with_latest:
            latest = sorted(t.__name__ for t in w.with_latest)
            raise GraphError(
                f"wire({w.data_type.__name__}).debounce()"
                f".with_latest({', '.join(latest)}): debounce handlers "
                f"are single-input (the fire signal carries one Data); "
                f"``.with_latest(...)`` is not supported. Resolve the "
                f"latest types upstream of emit and pass them through "
                f"the trigger Data instead."
            )
        if w.predicate is not None:
            raise GraphError(
                f"wire({w.data_type.__name__}).debounce().when(...): "
                f"emit() respects ``.when()`` predicates but the "
                f"DebounceReschedule path bypasses them — the two paths "
                f"would behave inconsistently. Filter upstream of emit, "
                f"or drop ``.when()`` / ``.debounce()`` — pick one."
            )
        if w.sinks:
            raise GraphError(
                f"wire({w.data_type.__name__}).debounce().to(Sink.*): "
                f"debounce wires must target exactly one @node consumer; "
                f"``Sink.*`` is not supported (the fire signal needs "
                f"business logic, not a passthrough mq publish)."
            )
        if w.sources:
            kinds = sorted({s.kind for s in w.sources})
            raise GraphError(
                f"wire({w.data_type.__name__}).debounce()"
                f".from_(Source.{','.join(kinds)}): debounce wires are "
                f"emit-driven; declarative ``Source.*`` is not supported."
            )
        if len(w.consumers) != 1:
            raise GraphError(
                f"wire({w.data_type.__name__}).debounce(): must have "
                f"exactly one consumer; got {len(w.consumers)} "
                f"({[c.__name__ for c in w.consumers]}). Debounce state "
                f"(redis latest+count keyed by DataType+key) cannot be "
                f"split across consumers."
            )
        if w.data_type in seen_debounce_types:
            raise GraphError(
                f"wire({w.data_type.__name__}).debounce(): "
                f"{w.data_type.__name__} already declared on another "
                f"debounce wire; redis state "
                f"(debounce:latest:{w.data_type.__name__}:{{key}}) would "
                f"collide. Each DataType can have at most one debounce "
                f"wire."
            )
        seen_debounce_types.add(w.data_type)

    # 4a) ``with_latest`` is implemented only on the in-process emit
    # path (``emit._resolve_inputs`` runs ``select_latest`` for each
    # ``with_latest`` type before invoking the consumer). The durable
    # handler in ``durable.py`` is a single-input dispatch:
    # ``publish_durable`` puts only the primary Data on the queue, and
    # ``_build_handler`` calls ``consumer(**{param_name: obj})`` with
    # one slot. Combining ``.durable()`` with ``.with_latest(...)``
    # therefore passes startup, then the first message reaches the
    # consumer with a missing kwarg and raises ``TypeError``. Refuse
    # the combination at boot until durable resolution learns to fan
    # the latest types in too.
    for w in wires:
        if w.durable and w.with_latest:
            latest = sorted(t.__name__ for t in w.with_latest)
            raise GraphError(
                f"wire({w.data_type.__name__}).with_latest({', '.join(latest)})"
                f".durable(): durable handlers do not yet inject "
                f"with_latest parameters — the queue carries only the "
                f"primary Data and the consumer would be invoked with "
                f"missing kwargs. Drop ``.durable()`` (keep the edge "
                f"in-process), or split into two edges: a durable "
                f"single-input consumer that re-emits an enriched Data, "
                f"with the with_latest join happening on the in-process "
                f"hop."
            )

    # 4b) ``Meta.transient = True`` means "no pg table, in-process only"
    # (the migrator skips DDL for transient Data, see ``migrator.py``).
    # ``.durable()`` requires the data type to round-trip through a
    # RabbitMQ queue *and* the consumer-side ``insert_idempotent`` for
    # at-least-once dedup — both demand a real pg table. The runtime
    # already exempts adoption-mode Data from idempotent (the row exists
    # by construction); transient is the opposite — there is no row and
    # there never will be — so the only honest answer is to refuse the
    # combination at boot rather than crash on first message with a
    # ``relation does not exist``.
    for w in wires:
        if not w.durable:
            continue
        meta = getattr(w.data_type, "Meta", None)
        if meta is not None and getattr(meta, "transient", False):
            raise GraphError(
                f"wire({w.data_type.__name__}).durable(): {w.data_type.__name__} "
                f"declares ``Meta.transient = True`` (no pg table), but "
                f"durable edges require a persisted table for "
                f"consumer-side ``insert_idempotent`` dedup. Either "
                f"remove ``transient`` so the runtime owns the table, "
                f"or drop ``.durable()`` and keep this edge in-process."
            )

    # 4e) A0 W2a: with_latest(X) 把 X 的第一个 Key 作为 join key 在 emit 时从
    # primary data 同名属性上读取（emit._resolve_inputs 的 getattr(data, key)）。
    # primary 类没有同名属性 → emit 首次触发才 raise RuntimeError。compile 时
    # 反射验证 join key 是 primary data 类的字段，把这个失败搬到 boot。
    # 放在 4a/4b 之后让 W3 (签名 strict equality) + W11 (durable+with_latest)
    # 先抛——它们的违反场景跟 W2a 的违反场景有重叠。
    from app.runtime.data import key_fields

    for w in wires:
        for t in w.with_latest:
            t_keys = key_fields(t)
            if not t_keys:
                raise GraphError(
                    f"wire({w.data_type.__name__}).with_latest({t.__name__}): "
                    f"{t.__name__} declares no Key field, cannot be used as a "
                    f"join target (with_latest joins on the latest target's "
                    f"first Key field)"
                )
            join_key = t_keys[0]
            primary_fields = w.data_type.model_fields
            if join_key not in primary_fields:
                raise GraphError(
                    f"wire({w.data_type.__name__}).with_latest({t.__name__}): "
                    f"join key {join_key!r} (first Key field of "
                    f"{t.__name__}) is not declared on "
                    f"{w.data_type.__name__} — emit-time getattr would fail. "
                    f"Add {join_key!r} to {w.data_type.__name__}, or change "
                    f"{t.__name__}'s first Key, or drop the with_latest."
                )

    # 4d) A0 W14: on_error != 'dlq' requires .durable(). in-process emit 路径
    # 上 consumer 抛异常直接 propagate 到 emit 调用方（emit.py docstring），
    # on_error 由 durable handler 在 _route_consumer_exception 落实，对
    # in-process 边没有意义。声明了非默认 on_error 但忘 .durable() 说明业务
    # 认知错位，boot 时拒掉。
    for w in wires:
        if w.on_error == "dlq":
            continue  # default — equivalent to not having declared anything
        if not w.durable:
            raise GraphError(
                f"wire({w.data_type.__name__}).on_error({w.on_error!r}): "
                f"on_error policies other than 'dlq' only take effect on "
                f"durable edges (the in-process path propagates consumer "
                f"exceptions directly to emit's caller). Add .durable(), "
                f"or drop .on_error() to fall back to the default."
            )

    # 5b) Phase 2 sink dispatch validation: every Sink.mq(name) must
    # reference a queue declared in ALL_ROUTES, otherwise the engine
    # wouldn't know which routing key to use when publishing (lane
    # fan-out + queue->rk binding live there). Catching this at compile
    # time means a typo surfaces at boot, not at the first emit.
    from app.infra.rabbitmq import ALL_ROUTES
    known_queues = {r.queue for r in ALL_ROUTES}
    sink_errors: list[str] = []
    for w in wires:
        for s in w.sinks:
            if s.kind == "mq":
                q = s.params["queue"]
                if q not in known_queues:
                    sink_errors.append(
                        f"wire({w.data_type.__name__}).to(Sink.mq({q!r})): "
                        f"queue not in ALL_ROUTES; sink dispatch needs a "
                        f"registered route to know the routing key. "
                        f"Add Route({q!r}, ...) to ALL_ROUTES first."
                    )
    if sink_errors:
        raise GraphError(
            "sink dispatch validation failed:\n  - " + "\n  - ".join(sink_errors)
        )

    # 6) HTTP source placement: ``register_http_sources`` only mounts on
    # the FastAPI main app (which is the agent-service deployment). A
    # wire whose source includes ``Source.http(...)`` must therefore have
    # its consumer running in DEFAULT_APP — otherwise the route returns
    # 202 to the client but emit() filters the consumer out by APP_NAME
    # and nothing happens. This refuses the cross-app HTTP wire at compile
    # time so the failure surfaces at boot, not as a silent 202.
    own_default = nodes_for_app(DEFAULT_APP)
    for w in wires:
        if not any(s.kind == "http" for s in w.sources):
            continue
        misplaced = [
            c.__name__ for c in w.consumers if c not in own_default
        ]
        if misplaced:
            raise GraphError(
                f"wire({w.data_type.__name__}).from_(Source.http(...)) "
                f"consumer(s) {sorted(misplaced)} are bound to non-default "
                f"app(s); HTTP sources are mounted only in "
                f"{DEFAULT_APP!r} (the FastAPI main process). Bind the "
                f"consumer to {DEFAULT_APP!r}, or expose a separate "
                f"main-service endpoint that publishes to MQ explicitly."
            )

    data_types: set[type[Data]] = {w.data_type for w in wires} | {
        t for w in wires for t in w.with_latest
    }
    nodes = {c for w in wires for c in w.consumers}
    return CompiledGraph(data_types=data_types, nodes=nodes, wires=wires)
