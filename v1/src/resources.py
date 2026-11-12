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
    """The resources one command occupies, possibly depending on a parameter.

    Three different things, kept apart because they end at different moments:

    * `claims` - occupied **while the command runs** and given back when it
      returns. The arm inside the deck during `place_on_OT`.
    * `acquires` - taken when the command runs and **kept after it returns**,
      until some later step releases it. The plate sitting in the reader once
      the robot has put it there.
    * `releases` - what this command gives back when it succeeds. The robot
      picking that plate up again.

    `by_parameter` carries the robot case: every transport is `ExecuteTask`, and
    what it occupies depends on *which task* - so the indirection sits where the
    variability actually is. A value maps either to a plain list (claims) or to
    an object with `claims` / `acquires` / `releases`.

    `exhaustive` says the `by_parameter` tables list **every** value there is.
    A step whose value is not listed is then refused at validation instead of
    being believed to occupy nothing: a task taught on the robot after the
    declaration was written is exactly the case where "not listed" does not
    mean "harmless".

    `control` marks a command that occupies nothing and must run *while* the
    instrument is busy: a stop, a pause, a status read. Without it the implicit
    per-instrument claim would queue `EmergencyStop` behind the recipe it is
    meant to interrupt, which is the opposite of what exclusion is for. It is a
    deliberate opt-out rather than an empty `claims` list, because an empty list
    is too easy to arrive at by accident for something that switches a safety
    rule off.
    """

    claims: Tuple[str, ...] = ()
    by_parameter: Mapping[str, Mapping[str, Tuple[str, ...]]] = field(default_factory=dict)
    control: bool = False
    #: Legacy spelling of acquire/release: resource id -> the command **of the
    #: same server** that gives it back. Parsed into `acquires` here and
    #: `releases` on the named command; kept so the declaration can be shown
    #: as it was written.
    holds: Mapping[str, str] = field(default_factory=dict)
    acquires: Tuple[str, ...] = ()
    releases: Tuple[str, ...] = ()
    acquires_by_parameter: Mapping[str, Mapping[str, Tuple[str, ...]]] = field(default_factory=dict)
    releases_by_parameter: Mapping[str, Mapping[str, Tuple[str, ...]]] = field(default_factory=dict)
    exhaustive: bool = False

    @staticmethod
    def _value_of(parameter: str, params: Mapping[str, Any]) -> Any:
        value = params.get(parameter)
        if value is None:
            # Case-insensitive second try: workflow JSON is written by hand
            # and `taskname` for `TaskName` is a common slip.
            lowered = {str(k).lower(): v for k, v in params.items()}
            value = lowered.get(parameter.lower())
        return value

    def _resolve(self, base: Tuple[str, ...],
                 table: Mapping[str, Mapping[str, Tuple[str, ...]]],
                 parameters: Optional[Mapping[str, Any]]) -> FrozenSet[str]:
        found: Set[str] = set(base)
        params = parameters or {}
        for parameter, mapping in table.items():
            value = self._value_of(parameter, params)
            if value is None:
                continue
            found.update(mapping.get(str(value), ()))
        return frozenset(found)

    def resolve(self, parameters: Optional[Mapping[str, Any]] = None) -> FrozenSet[str]:
        """What this command occupies while it runs, for the given parameters."""
        return self._resolve(self.claims, self.by_parameter, parameters)

    def resolve_acquires(self, parameters: Optional[Mapping[str, Any]] = None) -> FrozenSet[str]:
        """What this command keeps after it returns."""
        return self._resolve(self.acquires, self.acquires_by_parameter, parameters)

    def resolve_releases(self, parameters: Optional[Mapping[str, Any]] = None) -> FrozenSet[str]:
        """What this command gives back when it succeeds."""
        return self._resolve(self.releases, self.releases_by_parameter, parameters)

    def parameter_tables(self) -> Dict[str, Set[str]]:
        """parameter -> every value any of the three tables lists for it."""
        listed: Dict[str, Set[str]] = {}
        for table in (self.by_parameter, self.acquires_by_parameter, self.releases_by_parameter):
            for parameter, mapping in table.items():
                listed.setdefault(parameter, set()).update(mapping)
        return listed

    def unlisted(self, parameters: Optional[Mapping[str, Any]] = None) -> List[Tuple[str, str]]:
        """(parameter, value) pairs an `exhaustive` declaration does not cover.

        Empty unless the command is `exhaustive`. A step that gives none of the
        declared parameters is reported too: the declaration then says nothing
        about it, and "says nothing" must not read as "occupies nothing".
        """
        if not self.exhaustive:
            return []
        params = parameters or {}
        given = {
            parameter: self._value_of(parameter, params)
            for parameter in self.parameter_tables()
        }
        given = {k: v for k, v in given.items() if v is not None and str(v) != ""}
        tables = self.parameter_tables()
        if tables and not given:
            # None of the parameters the declaration is written in was given
            # at all. Whatever this step does, the declaration does not say.
            return [("/".join(sorted(tables)), "(not given)")]
        # Two spellings of one fact (TaskId and TaskName) are both declared;
        # a step is covered as soon as one of the values it gives is listed.
        if any(str(value) in tables[parameter] for parameter, value in given.items()):
            return []
        return [(parameter, str(value)) for parameter, value in given.items()]

    def held_resources(self) -> FrozenSet[str]:
        return frozenset(self.holds) | frozenset(self.acquires)

    def resolve_all(self) -> FrozenSet[str]:
        """Every resource this command could touch, for any parameter value.

        Distinct from `resolve`, which answers what one *step* occupies. The
        executor needs `resolve`; the discovery cross-check and the Settings
        panel need this one, because a claim that only appears for one task
        value still has to name a resource somebody provides.
        """
        found: Set[str] = set(self.claims) | set(self.acquires) | set(self.releases)
        for table in (self.by_parameter, self.acquires_by_parameter, self.releases_by_parameter):
            for mapping in table.values():
                for ids in mapping.values():
                    found.update(ids)
        found.update(self.holds)
        return frozenset(found)

    def to_dict(self) -> Dict[str, Any]:
        def _table(table):
            return {
                parameter: {value: list(ids) for value, ids in mapping.items()}
                for parameter, mapping in table.items()
            }
        return {
            "claims": list(self.claims),
            "by_parameter": _table(self.by_parameter),
            "control": self.control,
            "holds": dict(self.holds),
            "acquires": list(self.acquires),
            "releases": list(self.releases),
            "acquires_by_parameter": _table(self.acquires_by_parameter),
            "releases_by_parameter": _table(self.releases_by_parameter),
            "exhaustive": self.exhaustive,
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
            found.update(command.resolve_all())
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

    #: command -> resources it releases because another command `holds` them
    released_by: Dict[str, List[str]] = {}
    parsed: Dict[str, Dict[str, Any]] = {}

    for name, spec in commands.items():
        if not isinstance(spec, Mapping):
            declaration.problems.append(
                f"{label}resources.commands.{name} must be an object")
            continue
        where = f"resources.commands.{name}"
        claims = _resource_ids(spec.get("claims"), label, f"{where}.claims",
                               declaration.problems)
        acquires = _resource_ids(spec.get("acquires"), label, f"{where}.acquires",
                                 declaration.problems)
        releases = _resource_ids(spec.get("releases"), label, f"{where}.releases",
                                 declaration.problems)

        tables: Dict[str, Dict[str, Dict[str, Tuple[str, ...]]]] = {
            "claims": {}, "acquires": {}, "releases": {}}
        raw_by_parameter = spec.get("by_parameter") or {}
        if not isinstance(raw_by_parameter, Mapping):
            declaration.problems.append(f"{label}{where}.by_parameter must be an object")
            raw_by_parameter = {}
        for parameter, mapping in raw_by_parameter.items():
            if not isinstance(mapping, Mapping):
                declaration.problems.append(
                    f"{label}{where}.by_parameter.{parameter} "
                    "must map a parameter value to a list of resource ids")
                continue
            for value, entry in mapping.items():
                here = f"{where}.by_parameter.{parameter}.{value}"
                # A plain list is the claims; an object says which of the
                # three it means. An empty entry is still an entry: it is how
                # a task is declared to occupy nothing beyond the robot, which
                # an `exhaustive` declaration has to be able to say.
                if isinstance(entry, Mapping):
                    unknown = sorted(set(entry) - {"claims", "acquires", "releases"})
                    if unknown:
                        declaration.problems.append(
                            f"{label}{here} has unknown key(s) {', '.join(map(str, unknown))} "
                            "(expected claims, acquires, releases)")
                    parts = {kind: _resource_ids(entry.get(kind), label, f"{here}.{kind}",
                                                 declaration.problems)
                             for kind in ("claims", "acquires", "releases")}
                else:
                    parts = {"claims": _resource_ids(entry, label, here, declaration.problems),
                             "acquires": (), "releases": ()}
                tables["claims"].setdefault(str(parameter), {})[str(value)] = parts["claims"]
                for kind in ("acquires", "releases"):
                    if parts[kind]:
                        tables[kind].setdefault(str(parameter), {})[str(value)] = parts[kind]

        holds: Dict[str, str] = {}
        raw_holds = spec.get("holds") or {}
        if not isinstance(raw_holds, Mapping):
            declaration.problems.append(
                f"{label}{where}.holds must map a resource id "
                "to the command that releases it")
            raw_holds = {}
        for resource, releaser in raw_holds.items():
            if not isinstance(resource, str) or not RESOURCE_RE.match(resource.strip()):
                declaration.problems.append(
                    f"{label}{where}.holds has an unusable "
                    f"resource id {resource!r}")
                continue
            if not isinstance(releaser, str) or not releaser.strip():
                declaration.problems.append(
                    f"{label}{where}.holds.{resource} must name "
                    "the command that releases it")
                continue
            if releaser.strip() == str(name):
                declaration.problems.append(
                    f"{label}{where}.holds.{resource} is released "
                    "by itself, which would never free it")
                continue
            holds[resource.strip()] = releaser.strip()

        control = bool(spec.get("control", False))
        occupies = (claims or acquires or releases or holds
                    or any(ids for table in tables.values()
                           for mapping in table.values() for ids in mapping.values()))
        if control and occupies:
            declaration.problems.append(
                f"{label}{where} is marked control but also "
                "claims or holds resources; a control command occupies nothing")
            claims, acquires, releases, holds = (), (), (), {}
            tables = {"claims": {}, "acquires": {}, "releases": {}}

        # `holds: {X: Cmd}` is "this command acquires X, and Cmd on this same
        # server releases it". Scoping the releaser to the declaring server is
        # the point: matched by name alone, an UnloadPlate on a second reader
        # would free the first reader's tray.
        for resource, releaser in holds.items():
            if resource not in acquires:
                acquires = acquires + (resource,)
            released_by.setdefault(releaser, []).append(resource)

        parsed[str(name)] = dict(
            claims=claims, by_parameter=tables["claims"], control=control, holds=holds,
            acquires=acquires, releases=releases,
            acquires_by_parameter=tables["acquires"],
            releases_by_parameter=tables["releases"],
            exhaustive=bool(spec.get("exhaustive", False)),
        )

    for releaser, resources in released_by.items():
        target = parsed.setdefault(releaser, dict(
            claims=(), by_parameter={}, control=False, holds={}, acquires=(), releases=(),
            acquires_by_parameter={}, releases_by_parameter={}, exhaustive=False))
        if target["control"]:
            declaration.problems.append(
                f"{label}resources.commands.{releaser} is marked control but is named "
                "as the command that releases a held resource")
            continue
        target["releases"] = tuple(dict.fromkeys(tuple(target["releases"]) + tuple(resources)))

    for name, fields in parsed.items():
        declaration.commands[name] = CommandResources(**fields)

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
        declaration = self.declarations.get(instrument_id)
        spec = declaration.commands.get(command) if declaration is not None else None

        # A control command occupies nothing, not even its own instrument:
        # a stop that queues behind the run it is stopping is not a stop.
        if spec is not None and spec.control:
            return frozenset()

        found: Set[str] = {instrument_resource(instrument_id)}
        if declaration is None or not declaration.declared:
            found.add(UNDECLARED)
        if spec is not None:
            found.update(self._resolve_alias(r) for r in spec.resolve(parameters))
        return frozenset(found)

    def _spec(self, instrument_id: str, command: str) -> Optional[CommandResources]:
        declaration = self.declarations.get(instrument_id)
        spec = declaration.commands.get(command) if declaration is not None else None
        return None if (spec is None or spec.control) else spec

    def acquires_for(self, instrument_id: str, command: str,
                     parameters: Optional[Mapping[str, Any]] = None) -> FrozenSet[str]:
        """What a step keeps after it has finished, until something releases it."""
        spec = self._spec(instrument_id, command)
        if spec is None:
            return frozenset()
        return frozenset(self._resolve_alias(r) for r in spec.resolve_acquires(parameters))

    def releases_for(self, instrument_id: str, command: str,
                     parameters: Optional[Mapping[str, Any]] = None) -> FrozenSet[str]:
        """What a step gives back when it succeeds."""
        spec = self._spec(instrument_id, command)
        if spec is None:
            return frozenset()
        return frozenset(self._resolve_alias(r) for r in spec.resolve_releases(parameters))

    def unlisted_for(self, instrument_id: str, command: str,
                     parameters: Optional[Mapping[str, Any]] = None) -> List[Tuple[str, str]]:
        """Parameter values an exhaustive declaration does not cover."""
        spec = self._spec(instrument_id, command)
        return spec.unlisted(parameters) if spec is not None else []

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


def _plain(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def build_topology(declarations: Mapping[str, ResourceDeclaration],
                   config: Optional[ResourceConfig] = None,
                   names: Optional[Mapping[str, str]] = None) -> ResourceTopology:
    """Merge the three layers into one resolved topology.

    `declarations` maps instrument id to what that server declared (empty
    `ResourceDeclaration` for a server that said nothing). Resolution order:
    the server wins for what it declares, the config supplements where the
    server is silent, aliases apply to claims afterwards.

    The instrument id is the **registry key** - `mobile`, `tecan` - which is
    what a workflow writes and what `lab_config.yaml` uses under `servers:`.
    It is not the name the server announces (`GoFaGo Mobile Robot`): keyed on
    that, `resources.declare.mobile` matched nothing at the bench and the
    robot's claims silently never applied. `names` (id -> announced name) lets
    a `declare` entry written against the announced name still find its
    instrument, and a `declare` entry that finds none is now said out loud.
    """
    config = config or ResourceConfig()
    names = dict(names or {})
    topology = ResourceTopology(aliases=dict(config.aliases))
    topology.problems.extend(config.problems)

    # Which instrument each `declare` entry speaks for: by id, then by the
    # announced name, then by either with punctuation and case ignored.
    supplements: Dict[str, ResourceDeclaration] = {}
    for key, supplement in config.declare.items():
        target = None
        if key in declarations:
            target = key
        else:
            by_name = [i for i in declarations if names.get(i) == key]
            loose = [i for i in declarations
                     if _plain(key) in (_plain(i), _plain(names.get(i, "")))]
            candidates = by_name or loose
            if len(candidates) == 1:
                target = candidates[0]
        if target is None:
            known = ", ".join(sorted(declarations)) or "none"
            topology.problems.append(
                f"resources.declare.{key} matches no discovered instrument "
                f"(known: {known}) - what it declares is NOT applied")
            continue
        supplements[target] = supplement

    merged: Dict[str, ResourceDeclaration] = {}
    for instrument_id, declared in declarations.items():
        supplement = supplements.get(instrument_id)
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


def topology_of(instruments: Iterable[Tuple[str, str, Optional[ResourceDeclaration]]],
                config: Optional[ResourceConfig] = None) -> ResourceTopology:
    """The topology of a set of discovered instruments.

    `instruments` yields `(id, announced name, declaration)`. This is the one
    way the executor, the API and the CLI build a topology, so the Settings
    panel cannot show an exclusion the executor does not apply - which is what
    happened while each of them keyed it differently.
    """
    declarations: Dict[str, ResourceDeclaration] = {}
    names: Dict[str, str] = {}
    for instrument_id, name, declaration in instruments:
        declarations[str(instrument_id)] = declaration or ResourceDeclaration()
        names[str(instrument_id)] = str(name or instrument_id)
    return build_topology(declarations, config, names=names)


# ── Runtime: who holds what ───────────────────────────────────────────────────

class ResourceDeadlock(RuntimeError):
    """Every step still alive is waiting for a resource none of them will free."""


class ResourceWaitInterrupted(RuntimeError):
    """A wait for resources was called off: the run is being aborted."""


class ResourceLedger:
    """Grants and releases resource claims.

    A step acquires **all** of its claims as one atomic set and releases them
    when it finishes, so it never holds one while waiting for another and a
    cycle cannot form among step-scoped claims.

    A resource a command *acquires* outlives its step: `persist` moves it from
    the step to the ledger's own keeping, and it stays there until a step that
    declares it in `takeover` (a step that *releases* it) takes it back.
    Resources kept that way **can** deadlock - two plates each waiting for the
    position the other is in - so the ledger can be given `all_blocked`, a
    callable answering "is every live step now waiting here?". When it says
    yes, every waiter fails with `ResourceDeadlock` naming what it was waiting
    for, instead of the run hanging for ever with nothing on screen.

    Fair by arrival: waiters are woken in the order they asked.
    """

    #: Prefix of the holder name a kept resource is filed under.
    KEPT = "kept:"

    def __init__(self, max_concurrency: Optional[int] = None):
        self._held: Dict[str, str] = {}          # resource id -> holder
        self._holders: Dict[str, FrozenSet[str]] = {}   # holder -> its resources
        #: Holders that are running steps. Only these count against
        #: `max_concurrency`: a kept resource is not a step in progress, and
        #: counting it made a cap of 1 unsatisfiable the moment anything was
        #: kept - the run stopped for ever after the first hold.
        self._steps: Set[str] = set()
        #: kept resource -> who left it there, for messages
        self._origin: Dict[str, str] = {}
        self._waiters: List[Tuple[asyncio.Future, str, FrozenSet[str], FrozenSet[str]]] = []
        self._lock = asyncio.Lock()
        self._max_concurrency = max_concurrency if (max_concurrency or 0) > 0 else None
        self.all_blocked: Optional[Any] = None

    # ── introspection ────────────────────────────────────────────────────────

    @property
    def held(self) -> Dict[str, str]:
        """Snapshot of resource -> holder, for the API and the Settings panel."""
        return dict(self._held)

    def holder_of(self, resource: str) -> Optional[str]:
        return self._held.get(resource)

    def holdings(self) -> Dict[str, FrozenSet[str]]:
        """holder -> what it holds."""
        return dict(self._holders)

    def kept(self) -> Dict[str, str]:
        """resource -> who left it occupied, for everything kept across steps."""
        return {
            resource: self._origin.get(resource, holder)
            for resource, holder in self._held.items()
            if holder not in self._steps
        }

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    # ── taking ───────────────────────────────────────────────────────────────

    def _needed(self, wanted: FrozenSet[str], takeover: FrozenSet[str]) -> FrozenSet[str]:
        """`wanted`, plus whatever in `takeover` is currently occupied.

        A releasing step does not need the resource it releases to be busy -
        picking up a plate the operator loaded by hand is fine - but if it *is*
        occupied, the step has to be the one holding it while it works.
        """
        return wanted | frozenset(r for r in takeover if r in self._held)

    def _blocked_by(self, holder: str, wanted: FrozenSet[str],
                    takeover: FrozenSet[str]) -> List[str]:
        blocking: List[str] = []
        if (self._max_concurrency is not None and holder not in self._steps
                and len(self._steps) >= self._max_concurrency):
            blocking.append(f"the limit of {self._max_concurrency} step(s) at a time")
        for resource in sorted(self._needed(wanted, takeover)):
            current = self._held.get(resource)
            if current is None or current == holder:
                continue
            if current not in self._steps and resource in takeover:
                continue   # kept, and this step is one that releases it
            if current in self._steps:
                blocking.append(f"'{resource}' (in use by {current})")
            else:
                blocking.append(
                    f"'{resource}' (occupied since {self._origin.get(resource, current)}, "
                    "and nothing has released it)")
        return blocking

    async def acquire(self, holder: str, claims: Iterable[str],
                      takeover: Iterable[str] = ()) -> FrozenSet[str]:
        """Block until every claim is free, then take them all.

        `takeover` names resources this step releases: one of those that is
        being *kept* is handed to the step rather than waited for. Returns the
        resources taken over that way, so the caller can put them back if the
        step fails and the plate is, in fact, still there.
        """
        wanted = frozenset(claims)
        may_take = frozenset(takeover)
        while True:
            async with self._lock:
                if not self._blocked_by(holder, wanted, may_take):
                    taken: Set[str] = set()
                    for resource in self._needed(wanted, may_take):
                        previous = self._held.get(resource)
                        if previous is not None and previous != holder:
                            taken.add(resource)
                            left = self._holders.get(previous, frozenset()) - {resource}
                            if left:
                                self._holders[previous] = left
                            else:
                                self._holders.pop(previous, None)
                        self._held[resource] = holder
                    self._holders[holder] = (self._holders.get(holder, frozenset())
                                             | self._needed(wanted, may_take))
                    self._steps.add(holder)
                    return frozenset(taken)
                waiter: asyncio.Future = asyncio.get_running_loop().create_future()
                self._waiters.append((waiter, holder, wanted, may_take))
                check = self.all_blocked
                if check is not None and check(len(self._waiters)):
                    self._fail_waiters_locked(deadlock=True)
            try:
                await waiter
            except asyncio.CancelledError:
                async with self._lock:
                    self._waiters = [w for w in self._waiters if w[0] is not waiter]
                raise

    def _fail_waiters_locked(self, *, deadlock: bool, reason: str = "") -> None:
        waiters, self._waiters = self._waiters, []
        for waiter, holder, wanted, may_take in waiters:
            if waiter.done():
                continue
            if deadlock:
                blocking = self._blocked_by(holder, wanted, may_take)
                waiter.set_exception(ResourceDeadlock(
                    f"{holder} cannot start: it is waiting for "
                    f"{', '.join(blocking) or 'a resource'} and no step still "
                    "running can free it"))
            else:
                waiter.set_exception(ResourceWaitInterrupted(reason or "interrupted"))

    def check_deadlock(self, live: int) -> bool:
        """Fail every waiter if all `live` steps are waiting here."""
        if self._waiters and len(self._waiters) >= live:
            self._fail_waiters_locked(deadlock=True)
            return True
        return False

    def interrupt(self, reason: str = "the run is being aborted") -> int:
        """Call off every wait. Returns how many steps were waiting.

        An abort has to reach a step that is queued for a resource, not only
        one that is executing: cancelling the running step frees *its* claims,
        but nothing frees a kept resource, so a step queued behind one would
        otherwise sit there and the run would never end.
        """
        count = len(self._waiters)
        self._fail_waiters_locked(deadlock=False, reason=reason)
        return count

    # ── keeping and giving back ──────────────────────────────────────────────

    async def persist(self, holder: str, resources: Iterable[str],
                      origin: str = "") -> None:
        """Keep `resources` occupied after `holder` has finished.

        The move happens under the ledger lock, so there is no instant in which
        the position is free with a plate still on it.
        """
        wanted = frozenset(resources)
        if not wanted:
            return
        async with self._lock:
            for resource in wanted:
                keeper = f"{self.KEPT}{resource}"
                previous = self._held.get(resource)
                if previous is not None and previous != keeper:
                    left = self._holders.get(previous, frozenset()) - {resource}
                    if left:
                        self._holders[previous] = left
                    else:
                        self._holders.pop(previous, None)
                self._held[resource] = keeper
                self._holders[keeper] = frozenset({resource})
                if origin or resource not in self._origin:
                    self._origin[resource] = origin or holder

    async def transfer(self, from_holder: str, to_holder: str,
                       resources: Iterable[str]) -> None:
        """Move resources from one holder to another without letting go.

        Resources `from_holder` does not hold are taken outright. `to_holder`
        is not a running step and does not count against `max_concurrency`.
        """
        wanted = frozenset(resources)
        if not wanted:
            return
        async with self._lock:
            for resource in wanted:
                self._held[resource] = to_holder
                self._origin.setdefault(resource, from_holder)
            kept = self._holders.get(from_holder, frozenset()) - wanted
            if kept:
                self._holders[from_holder] = kept
            else:
                self._holders.pop(from_holder, None)
            self._holders[to_holder] = self._holders.get(to_holder, frozenset()) | wanted
            self._steps.discard(to_holder)

    async def release(self, holder: str) -> None:
        """Give back everything `holder` took, and wake whoever is waiting."""
        async with self._lock:
            for resource in self._holders.pop(holder, frozenset()):
                if self._held.get(resource) == holder:
                    del self._held[resource]
                    self._origin.pop(resource, None)
            self._steps.discard(holder)
            waiters, self._waiters = self._waiters, []
        for waiter, _holder, _wanted, _may_take in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def release_kept(self, resource: str) -> None:
        """Free one kept resource - at the end of a run, when nothing did."""
        await self.release(self._held.get(resource, f"{self.KEPT}{resource}"))

    def _can_take(self, wanted: FrozenSet[str]) -> bool:
        return not self._blocked_by("", frozenset(wanted), frozenset())
