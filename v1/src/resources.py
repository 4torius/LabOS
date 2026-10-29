"""Which physical things a step occupies, and who is allowed to say so.

A SiLA2 feature definition says what commands an instrument accepts. It does
not say what those commands physically occupy, and the orchestrator needs that
to know when two steps must not run together.

The instrument is not a fine enough unit. The mobile robot's tasks are called
`place_on_OT`, `pick_from_tecan`, `place_on_tecan` — a step **on the robot**
that occupies **somebody else's deck or tray**. Keyed on the step's instrument,
a lock would let the Opentrons pipette into a deck with an arm inside it and be
working as designed while it happened. So the unit of exclusion is the physical
thing, whoever reaches for it: a resource id like `deck:opentrons`.

Resources come from three places, chosen by who is able to know the fact
(the full argument is in `docs/RESOURCE_MODEL.md`):

1. **The server**, in its `library_catalog.json`, served over
   `LabOSLibraryProvider.LibraryCatalog`. The normal case: an instrument knows
   its own geometry. Zero configuration.
2. **`lab_config.yaml`**, for servers that *cannot* declare — a vendor SiLA2
   server does not know an Opentrons exists and cannot be modified. A
   supplement, never an override.
3. **Aliases**, for the one genuinely deployment-variable binding: tasks are
   taught relative to a workstation's ArUco marker, so swapping the instrument
   behind a marker changes the claim target while the taught motion stays valid.

`provides` is separate from `claims` on purpose. With claims alone, two servers
would agree on a name the way two processes agree on a lockfile path — and a
typo (`deck:OT` against `deck:opentrons`) would produce no error and no mutual
exclusion, silently. A safety mechanism that does not engage without saying so
is worse than none. With an owner per name, an unprovided claim is a
configuration error that surfaces at discovery.

Like `catalog.py`, this module reads documents that arrive over the network
from another machine running another version. It is untrusted input: a
malformed entry costs that entry and is reported in `problems`, and nothing
raises out of here. The one thing that is *not* forgiving is an unresolved
claim, which fails closed — see `ResourceTopology.unresolved`.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

#: A resource id is `kind:name` — `deck:opentrons`, `instrument:tecan`.
#: The kind is a lowercase category so the Settings panel can group them; the
#: name is whatever the owner calls the thing.
RESOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}:[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")

#: An alias is a bare name (no colon), to keep it distinguishable at a glance
#: from the concrete id it resolves to.
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")

#: Kind prefix for the implicit per-instrument claim every step carries.
INSTRUMENT_KIND = "instrument"

#: Claimed by every step whose server declares no resources at all, so that
#: undeclared steps serialise against each other instead of all becoming
#: mutually disjoint — which would have made this module *introduce* the hazard
#: it exists to remove. Concurrency is earned by declaring, not configured.
UNDECLARED = "lab:undeclared"


def instrument_resource(instrument_id: str) -> str:
    """The implicit claim a step carries for its own instrument."""
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(instrument_id or "unknown")).strip("_")
    return f"{INSTRUMENT_KIND}:{safe or 'unknown'}"


# ── What one command occupies ─────────────────────────────────────────────────

@dataclass(frozen=True)
class CommandResources:
    """The resources one command claims, possibly depending on a parameter.

    `by_parameter` carries the robot case: every transport is `ExecuteTask`, and
    what it occupies depends on *which task* — so the indirection sits where the
    variability actually is.
    """

    claims: Tuple[str, ...] = ()
    by_parameter: Mapping[str, Mapping[str, Tuple[str, ...]]] = field(default_factory=dict)

    def resolve(self, parameters: Optional[Mapping[str, Any]] = None) -> FrozenSet[str]:
        """Every resource this command occupies for the given parameters."""
        found: Set[str] = set(self.claims)
        params = parameters or {}
        for parameter, mapping in self.by_parameter.items():
            value = params.get(parameter)
            if value is None:
                # Case-insensitive second try: workflow JSON is written by hand
                # and `taskname` for `TaskName` is a common slip.
                lowered = {str(k).lower(): v for k, v in params.items()}
                value = lowered.get(parameter.lower())
            if value is None:
                continue
            found.update(mapping.get(str(value), ()))
        return frozenset(found)

    def resolve_all(self) -> FrozenSet[str]:
        """Every resource this command could claim, for any parameter value.

        Distinct from `resolve`, which answers what one *step* occupies. The
        executor needs `resolve`; the discovery cross-check and the Settings
        panel need this one, because a claim that only appears for one task
        value still has to name a resource somebody provides.
        """
        found: Set[str] = set(self.claims)
        for mapping in self.by_parameter.values():
            for ids in mapping.values():
                found.update(ids)
        return frozenset(found)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claims": list(self.claims),
            "by_parameter": {
                parameter: {value: list(ids) for value, ids in mapping.items()}
                for parameter, mapping in self.by_parameter.items()
            },
        }


@dataclass
class ResourceDeclaration:
    """One server's statement about what it owns and what its commands occupy."""

    provides: Tuple[str, ...] = ()
    commands: Dict[str, CommandResources] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)
    #: False when the document carried no `resources` section at all, which is
    #: what makes a step claim UNDECLARED. An empty-but-present section is a
    #: deliberate "I occupy nothing beyond myself" and is believed.
    declared: bool = False

    def every_claim(self) -> FrozenSet[str]:
        """Every resource id mentioned anywhere, for cross-checking."""
        found: Set[str] = set()
        for command in self.commands.values():
            found.update(command.claims)
            for mapping in command.by_parameter.values():
                for ids in mapping.values():
                    found.update(ids)
        return frozenset(found)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provides": list(self.provides),
            "commands": {name: c.to_dict() for name, c in self.commands.items()},
            "declared": self.declared,
            "problems": list(self.problems),
        }


def _resource_ids(raw: Any, label: str, what: str,
                  problems: List[str]) -> Tuple[str, ...]:
    """Validate a list of resource ids, dropping and reporting bad ones."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        problems.append(f"{label}{what} must be a list of resource ids")
        return ()
    good: List[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not RESOURCE_RE.match(entry.strip()):
            problems.append(
                f"{label}{what} contains an unusable resource id {entry!r} "
                "(expected 'kind:name', e.g. 'deck:opentrons')")
            continue
        value = entry.strip()
        if value not in good:
            good.append(value)
    return tuple(good)


def parse_declaration(raw: Any, source: str = "") -> ResourceDeclaration:
    """Read the `resources` section of a catalog. Never raises."""
    declaration = ResourceDeclaration()
    label = f"{source}: " if source else ""

    if raw is None:
        return declaration
    if not isinstance(raw, Mapping):
        declaration.problems.append(f"{label}resources must be a JSON object")
        return declaration

    declaration.declared = True
    declaration.provides = _resource_ids(
        raw.get("provides"), label, "resources.provides", declaration.problems)

    commands = raw.get("commands") or {}
    if not isinstance(commands, Mapping):
        declaration.problems.append(f"{label}resources.commands must be an object")
        return declaration

    for name, spec in commands.items():
        if not isinstance(spec, Mapping):
            declaration.problems.append(
                f"{label}resources.commands.{name} must be an object")
            continue
        claims = _resource_ids(
            spec.get("claims"), label, f"resources.commands.{name}.claims",
            declaration.problems)

        by_parameter: Dict[str, Dict[str, Tuple[str, ...]]] = {}
        raw_by_parameter = spec.get("by_parameter") or {}
        if not isinstance(raw_by_parameter, Mapping):
            declaration.problems.append(
                f"{label}resources.commands.{name}.by_parameter must be an object")
            raw_by_parameter = {}
        for parameter, mapping in raw_by_parameter.items():
            if not isinstance(mapping, Mapping):
                declaration.problems.append(
                    f"{label}resources.commands.{name}.by_parameter.{parameter} "
                    "must map a parameter value to a list of resource ids")
                continue
            values: Dict[str, Tuple[str, ...]] = {}
            for value, ids in mapping.items():
                resolved = _resource_ids(
                    ids, label,
                    f"resources.commands.{name}.by_parameter.{parameter}.{value}",
                    declaration.problems)
                if resolved:
                    values[str(value)] = resolved
            if values:
                by_parameter[str(parameter)] = values

        declaration.commands[str(name)] = CommandResources(
            claims=claims, by_parameter=by_parameter)

    return declaration


# ── The config layers ─────────────────────────────────────────────────────────

@dataclass
class ResourceConfig:
    """`lab_config.yaml`'s `resources:` section — layers 2 and 3.

    Only what no server can speak for: declarations on behalf of servers that
    cannot declare (a vendor SiLA2 server knows nothing of this lab), and
    aliases for the one binding that is genuinely deployment-variable.
    """

    declare: Dict[str, ResourceDeclaration] = field(default_factory=dict)
    aliases: Dict[str, str] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "declare": {k: v.to_dict() for k, v in self.declare.items()},
            "aliases": dict(self.aliases),
            "problems": list(self.problems),
        }


def parse_config(raw: Any) -> ResourceConfig:
    """Read `lab_config.yaml`'s `resources:` section. Never raises."""
    config = ResourceConfig()
    if raw is None:
        return config
    if not isinstance(raw, Mapping):
        config.problems.append("resources: must be a mapping")
        return config

    declare = raw.get("declare") or {}
    if not isinstance(declare, Mapping):
        config.problems.append("resources.declare must be a mapping of server key to declaration")
    else:
        for key, spec in declare.items():
            declaration = parse_declaration(spec, source=f"resources.declare.{key}")
            config.problems.extend(declaration.problems)
            declaration.problems = []
            config.declare[str(key)] = declaration

    aliases = raw.get("aliases") or {}
    if not isinstance(aliases, Mapping):
        config.problems.append("resources.aliases must be a mapping of alias to resource id")
        return config
    for alias, target in aliases.items():
        name = str(alias).strip()
        if not ALIAS_RE.match(name):
            config.problems.append(
                f"resources.aliases key {alias!r} is unusable "
                "(a bare name, no colon, e.g. 'station_1')")
            continue
        if not isinstance(target, str) or not RESOURCE_RE.match(target.strip()):
            config.problems.append(
                f"resources.aliases.{name} must point at a resource id "
                f"like 'deck:opentrons', got {target!r}")
            continue
        config.aliases[name] = target.strip()

    # An alias pointing at another alias would need chain resolution, which
    # invites cycles for no benefit. One step only, and say so.
    for name, target in list(config.aliases.items()):
        bare = target.split(":", 1)[1] if ":" in target else target
        if bare in config.aliases:
            config.problems.append(
                f"resources.aliases.{name} points at {target!r}, which is itself "
                "an alias — chains are not resolved; point it at a real resource")
    return config


# ── The resolved whole ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UnresolvedClaim:
    """A claim naming a resource nobody provides. Fails closed."""

    instrument: str
    command: str
    resource: str

    def __str__(self) -> str:
        return (f"{self.instrument}.{self.command} claims '{self.resource}', "
                "which no instrument provides and no alias translates")


@dataclass
class ResourceTopology:
    """Everything resolved: who provides what, who claims what, what is wrong.

    Built at discovery and rebuilt whenever the registry changes. `problems` is
    advisory and shown in the Settings panel; `unresolved` is not — a workflow
    touching an unresolved claim fails validation.
    """

    #: resource id -> instrument id that declares itself its owner
    providers: Dict[str, str] = field(default_factory=dict)
    #: instrument id -> its resolved declaration
    declarations: Dict[str, ResourceDeclaration] = field(default_factory=dict)
    aliases: Dict[str, str] = field(default_factory=dict)
    unresolved: List[UnresolvedClaim] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    #: instrument ids whose server declared nothing, so their steps serialise
    undeclared: Tuple[str, ...] = ()

    # ── the two questions the executor asks ──────────────────────────────────

    def claims_for(self, instrument_id: str, command: str,
                   parameters: Optional[Mapping[str, Any]] = None) -> FrozenSet[str]:
        """Every resource a step occupies, implicit claims included.

        Always contains `instrument:<id>`, so an instrument is serialised
        against itself whether or not anybody declared it. Contains UNDECLARED
        when the server declared no resources at all, which keeps undeclared
        steps serialised against each other rather than mutually disjoint.
        """
        found: Set[str] = {instrument_resource(instrument_id)}
        declaration = self.declarations.get(instrument_id)
        if declaration is None or not declaration.declared:
            found.add(UNDECLARED)
        if declaration is not None:
            spec = declaration.commands.get(command)
            if spec is not None:
                found.update(self._resolve_alias(r) for r in spec.resolve(parameters))
        return frozenset(found)

    def conflicts(self, left: Iterable[str], right: Iterable[str]) -> bool:
        """Whether two steps contend. Today: a shared resource.

        The single place a richer rule would go — cross-resource mutex groups,
        say. Deliberately not built: the motivating example turned out to be
        covered by the implicit per-instrument claim, and the rest are hazards
        this lab does not have.
        """
        return bool(frozenset(left) & frozenset(right))

    # ── introspection, for the API and the Settings panel ────────────────────

    def claimants(self) -> Dict[str, List[str]]:
        """resource id -> the `instrument.command` strings that claim it."""
        out: Dict[str, List[str]] = {}
        for instrument_id, declaration in self.declarations.items():
            for command, spec in declaration.commands.items():
                for resource in spec.resolve_all():
                    out.setdefault(self._resolve_alias(resource), []).append(
                        f"{instrument_id}.{command}")
        return {k: sorted(set(v)) for k, v in out.items()}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "providers": dict(self.providers),
            "claimants": self.claimants(),
            "aliases": dict(self.aliases),
            "declarations": {k: v.to_dict() for k, v in self.declarations.items()},
            "undeclared": list(self.undeclared),
            "unresolved": [
                {"instrument": u.instrument, "command": u.command,
                 "resource": u.resource, "message": str(u)}
                for u in self.unresolved
            ],
            "problems": list(self.problems),
            "ok": not self.unresolved,
        }

    def _resolve_alias(self, resource: str) -> str:
        bare = resource.split(":", 1)[1] if ":" in resource else resource
        return self.aliases.get(bare, self.aliases.get(resource, resource))


def build_topology(declarations: Mapping[str, ResourceDeclaration],
                   config: Optional[ResourceConfig] = None) -> ResourceTopology:
    """Merge the three layers into one resolved topology.

    `declarations` maps instrument id to what that server declared (empty
    `ResourceDeclaration` for a server that said nothing). Resolution order:
    the server wins for what it declares, the config supplements where the
    server is silent, aliases apply to claims afterwards.
    """
    config = config or ResourceConfig()
    topology = ResourceTopology(aliases=dict(config.aliases))
    topology.problems.extend(config.problems)

    merged: Dict[str, ResourceDeclaration] = {}
    for instrument_id, declared in declarations.items():
        supplement = config.declare.get(instrument_id)
        if declared.declared:
            merged[instrument_id] = declared
            if supplement is not None:
                # Rule 1: the instrument is the authority on itself. Ignoring
                # the config here is what keeps plug-and-play — an instrument
                # that declares cannot be contradicted from central config.
                overlap = sorted(set(supplement.commands) & set(declared.commands))
                if overlap or supplement.provides:
                    topology.problems.append(
                        f"resources.declare.{instrument_id} is ignored: the server "
                        f"declares its own resources"
                        + (f" (overlapping commands: {', '.join(overlap)})" if overlap else ""))
            continue

        if supplement is None:
            merged[instrument_id] = declared
            continue

        # Rule 2: the config speaks for a server that cannot.
        merged[instrument_id] = ResourceDeclaration(
            provides=supplement.provides,
            commands=dict(supplement.commands),
            problems=list(declared.problems),
            declared=True,
        )

    topology.declarations = merged
    topology.undeclared = tuple(sorted(
        instrument_id for instrument_id, declaration in merged.items()
        if not declaration.declared))

    # Providers. Every instrument implicitly provides its own id, so the
    # implicit claim is never unresolved.
    for instrument_id, declaration in merged.items():
        for resource in (instrument_resource(instrument_id),) + tuple(declaration.provides):
            owner = topology.providers.get(resource)
            if owner is not None and owner != instrument_id:
                topology.problems.append(
                    f"resource '{resource}' is provided by both '{owner}' and "
                    f"'{instrument_id}' — one owner per resource")
                continue
            topology.providers[resource] = instrument_id
        topology.problems.extend(declaration.problems)

    # An alias must point at something that exists, or it silently resolves a
    # claim to a resource nobody provides — which the cross-check below would
    # then report against the claimant rather than the alias.
    for alias, target in topology.aliases.items():
        if target not in topology.providers:
            topology.problems.append(
                f"resources.aliases.{alias} points at '{target}', which no "
                "instrument provides")

    # Rule 4: fail closed. Every claim must name a provided resource.
    for instrument_id, declaration in merged.items():
        for command, spec in declaration.commands.items():
            for resource in spec.resolve_all():
                resolved = topology._resolve_alias(resource)
                if resolved not in topology.providers:
                    topology.unresolved.append(
                        UnresolvedClaim(instrument_id, command, resource))

    topology.unresolved.sort(key=lambda u: (u.instrument, u.command, u.resource))
    return topology


# ── Runtime: who holds what ───────────────────────────────────────────────────

class ResourceLedger:
    """Grants and releases resource claims for the duration of a step.

    A step acquires **all** of its claims as one atomic set and releases them
    when it finishes, so it never holds one while waiting for another and a
    cycle cannot form: step-scoped claims cannot deadlock, whatever the
    workflow looks like. That property is why claims held *across* steps
    (`holds` / `release_on`, for the plate sitting in the reader between
    `PlateIn` and `PlateOut`) are a later phase rather than part of this one.

    Fair by arrival: waiters are woken in the order they asked, so a step
    needing a busy resource cannot be starved by a stream of steps needing a
    free one.
    """

    def __init__(self, max_concurrency: Optional[int] = None):
        self._held: Dict[str, str] = {}          # resource id -> holder
        self._holders: Dict[str, FrozenSet[str]] = {}   # holder -> its resources
        self._waiters: List[asyncio.Future] = []
        self._lock = asyncio.Lock()
        self._max_concurrency = max_concurrency if (max_concurrency or 0) > 0 else None

    @property
    def held(self) -> Dict[str, str]:
        """Snapshot of resource -> holder, for the API and the Settings panel."""
        return dict(self._held)

    def holder_of(self, resource: str) -> Optional[str]:
        return self._held.get(resource)

    async def acquire(self, holder: str, claims: Iterable[str]) -> None:
        """Block until every claim is free, then take them all."""
        wanted = frozenset(claims)
        while True:
            async with self._lock:
                if self._can_take(wanted):
                    for resource in wanted:
                        self._held[resource] = holder
                    self._holders[holder] = wanted
                    return
                waiter: asyncio.Future = asyncio.get_running_loop().create_future()
                self._waiters.append(waiter)
            try:
                await waiter
            except asyncio.CancelledError:
                async with self._lock:
                    if waiter in self._waiters:
                        self._waiters.remove(waiter)
                raise

    async def release(self, holder: str) -> None:
        """Give back everything `holder` took, and wake whoever is waiting."""
        async with self._lock:
            for resource in self._holders.pop(holder, frozenset()):
                if self._held.get(resource) == holder:
                    del self._held[resource]
            waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    def _can_take(self, wanted: FrozenSet[str]) -> bool:
        if self._max_concurrency is not None and len(self._holders) >= self._max_concurrency:
            return False
        return not any(resource in self._held for resource in wanted)
