"""
Tip Tracker - Tracks tip consumption across runs
================================================

Persistent tip usage tracking for deterministic tip management.
Supports crash recovery and partial run analysis.
"""

import json
import logging
import os
import tempfile
import threading
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)


class TipTracker:
    """
    Tracks tip usage for deterministic tip management.
    
    Features:
        - Persistent state (JSON file)
        - Multi-rack type support
        - Reset/refill capability
        - Availability checking
    """
    
    def __init__(self, state_file: str = "./tip_state.json"):
        """
        Initialize TipTracker.
        
        Args:
            state_file: Path to JSON file for persisting state
        """
        self._file_path = state_file
        self._usage: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._load()
        
    def _load(self):
        """Load tip state from file."""
        if os.path.exists(self._file_path):
            try:
                with open(self._file_path, 'r', encoding='utf-8') as f:
                    self._usage = json.load(f)
                logger.debug(f"Tip state loaded: {self._usage}")
            except Exception as e:
                logger.warning(f"Failed to load tip state: {e}")
                self._usage = {}
        else:
            self._usage = {}
    
    def reload(self):
        """Reload tip state from file (external changes)."""
        self._load()
            
    def save(self):
        """Save tip state to file atomically (write to temp file, then rename).

        A crash or power loss mid-write must not corrupt the only copy of
        tip usage data; os.replace() is atomic on both POSIX and Windows.
        """
        try:
            directory = os.path.dirname(self._file_path) or "."
            fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tip_state_", suffix=".tmp")
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(self._usage, f, indent=2)
                os.replace(tmp_path, self._file_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
        except Exception as e:
            logger.error(f"Failed to save tip state: {e}")
            
    def get_usage(self, rack_type: str) -> int:
        """
        Get current usage count for a rack type.

        NOTE: Always reloads from file to capture external changes (webapp refill).

        Args:
            rack_type: Opentrons load name of the tip rack

        Returns:
            Number of tips used
        """
        with self._lock:
            self._load()
            return self._usage.get(rack_type, 0)

    def add_usage(self, rack_type: str, count: int):
        """
        Add to usage count for a rack type.

        Args:
            rack_type: Opentrons load name of the tip rack
            count: Number of tips to add
        """
        with self._lock:
            self._load()
            if rack_type not in self._usage:
                self._usage[rack_type] = 0
            self._usage[rack_type] += count
            self.save()
        logger.debug(f"Tip usage: {rack_type} += {count} (total: {self._usage[rack_type]})")
        
    def reset(self, rack_type: str):
        """
        Reset usage count for a rack type (refill).

        Args:
            rack_type: Opentrons load name of the tip rack
        """
        with self._lock:
            self._load()
            self._usage[rack_type] = 0
            self.save()
        logger.info(f"Tip rack reset: {rack_type}")

    def reset_all(self):
        """Reset all tip counts."""
        with self._lock:
            self._load()
            for key in self._usage:
                self._usage[key] = 0
            self.save()
        logger.info("All tip racks reset")

    def get_all_usage(self) -> Dict[str, int]:
        """Get all usage data."""
        with self._lock:
            return self._usage.copy()
        
    def get_tracked_types(self) -> List[str]:
        """Get list of tracked rack types."""
        return list(self._usage.keys())
        
    def check_availability(self, rack_type: str, required: int, capacity: int = 96) -> bool:
        """
        Check if enough tips are available.
        
        Args:
            rack_type: Opentrons load name
            required: Number of tips needed
            capacity: Total capacity of rack (default 96)
            
        Returns:
            True if enough tips available
        """
        used = self.get_usage(rack_type)
        available = capacity - used
        return available >= required
        
    def to_json(self) -> str:
        """Get state as JSON string."""
        return json.dumps(self._usage, indent=2)

    def sync_with_allowed_types(self, allowed_types: Set[str]) -> Dict[str, List[str]]:
        """
        Synchronize persisted tip state with a set of allowed rack load names.

        Policy:
            - Keep counters for rack types present in both current state and allowed set
            - Initialize new allowed rack types to 0
            - Rack types no longer present in allowed set are left untouched (not
              deleted) so a real, partially-used count survives a config switch
              away and back (e.g. HAL A -> B -> A must not reset A to full)

        Args:
            allowed_types: Set of rack load names currently present in HAL

        Returns:
            Summary dict with keys: kept, added, removed (removed = no longer
            active in this HAL, but their counters are preserved, not deleted)
        """
        self._load()  # Reload first to avoid overwriting external changes.

        current_keys = set(self._usage.keys())
        kept = sorted(list(current_keys & allowed_types))
        added = sorted(list(allowed_types - current_keys))
        removed = sorted(list(current_keys - allowed_types))

        new_usage: Dict[str, int] = dict(self._usage)  # preserve everything, including "removed"
        for rack_type in added:
            new_usage[rack_type] = 0

        self._usage = new_usage
        self.save()

        return {
            "kept": kept,
            "added": added,
            "removed": removed,
        }


class MountedTipTracker:
    """
    Persists, per pipette mount, whether a physical tip is believed to still
    be attached - reconstructed from each run's own command log.

    Neither of the two "obvious" sources can answer this across separate
    runs on their own:
        - `has_tip` is software-only and resets with every fresh protocol
          context (confirmed in Opentrons' own docs: "determined logically
          by the API, not by detecting the physical presence of a tip...
          even on Flex").
        - `detect_tip_presence()` isn't part of the public Python Protocol
          API at all (confirmed on hardware: AttributeError on this robot's
          installed robot-server, and absent from the official API docs).

    So instead: every run's command log (`get_run_commands`) is authoritative
    for what actually executed, even on a run that failed partway through.
    Replaying its pickUpTip/dropTip/dropTipInPlace commands in order
    reconstructs the true final has_tip state per mount - without needing
    any hardware sensor.

    Safety rule: any doubt (missing file, unreadable command log, a run that
    never gets reconciled - e.g. the server process itself dying mid-run)
    must resolve to "assume a tip is mounted", never to "assume clear". A
    spurious drop is harmless (Force DropTip is a safe no-op with nothing
    attached); a skipped drop when a tip really is there is a collision risk.
    Callers should call `mark_unconfirmed()` for every mount a run is about
    to use *before* starting it, and `mark_from_commands()` after - so a
    crash in between leaves that mount at "assume present" for next time.
    """

    def __init__(self, state_file: str = "./tip_mount_state.json"):
        self._file_path = state_file
        self._state: Dict[str, bool] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if os.path.exists(self._file_path):
            try:
                with open(self._file_path, 'r', encoding='utf-8') as f:
                    self._state = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load mounted-tip state: {e}")
                self._state = {}
        else:
            self._state = {}

    def save(self):
        """Save atomically (write to temp file, then os.replace)."""
        try:
            directory = os.path.dirname(self._file_path) or "."
            fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tip_mount_state_", suffix=".tmp")
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(self._state, f, indent=2)
                os.replace(tmp_path, self._file_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
        except Exception as e:
            logger.error(f"Failed to save mounted-tip state: {e}")

    def might_have_tip(self, mount: str) -> bool:
        """True unless we have confirmed, persisted knowledge the mount is clear."""
        with self._lock:
            self._load()
            return self._state.get(mount, True)

    def mark_unconfirmed(self, mounts: List[str]):
        """Call before a run starts: pessimistically assume every mount it
        uses might have a tip, until mark_from_commands() proves otherwise."""
        if not mounts:
            return
        with self._lock:
            self._load()
            for mount in mounts:
                self._state[mount] = True
            self.save()

    def mark_from_commands(self, commands: List[Dict[str, Any]], pipette_mounts: Dict[str, str]):
        """Reconstruct final has_tip state per mount from a run's actual
        command log and persist it. `pipette_mounts` maps pipetteId -> mount
        (see reconstruct_tip_mount_state)."""
        final_state = reconstruct_tip_mount_state(commands, pipette_mounts)
        if not final_state:
            return
        with self._lock:
            self._load()
            self._state.update(final_state)
            self.save()


def reconstruct_tip_mount_state(
    commands: List[Dict[str, Any]],
    pipette_mounts: Dict[str, str],
) -> Dict[str, bool]:
    """
    Replay a run's command log to determine, per pipette mount, whether it
    ended the run with a tip attached.

    `pipette_mounts` must map pipetteId -> mount, sourced from the run's own
    top-level detail (`GET /runs/{id}` -> data.pipettes via
    RobotClient.get_run_detail()) - NOT from each command's "result" field.
    Confirmed on this robot's API: the /runs/{id}/commands *list* endpoint
    never populates "result" (a loadPipette command there carries "params"
    only, no resulting pipetteId), so deriving the map from command results
    silently produces an empty map and reconciliation never learns anything
    (the same pre-existing gap already affects `get_actual_tip_usage()`'s
    loadLabware/labwareId lookup, which is why it always falls through to
    "Applying full planned usage for safety" in practice).
    """
    state: Dict[str, bool] = {}
    for cmd in commands:
        if cmd.get("status") != "succeeded":
            continue
        command_type = cmd.get("commandType")
        if command_type not in ("pickUpTip", "dropTip", "dropTipInPlace"):
            continue
        pipette_id = cmd.get("params", {}).get("pipetteId", "")
        mount = pipette_mounts.get(pipette_id)
        if not mount:
            continue
        state[mount] = command_type == "pickUpTip"

    return state


def calculate_tips_from_recipe(recipe: Dict[str, Any]) -> Dict[str, int]:
    """
    Calculate tip requirements from a JSON recipe.
    
    Analyzes recipe steps to predict tip consumption.
    
    Args:
        recipe: JSON recipe dictionary
        
    Returns:
        Dict mapping rack load names to required tip counts
    """
    usage: Dict[str, int] = {}
    
    # Find default tip rack - check both Labware (post-HAL) and Requirements.Labware (pre-HAL)
    default_rack = ""
    
    # Check Labware (post-HAL format with LoadName)
    if "Labware" in recipe:
        for key, value in recipe["Labware"].items():
            if isinstance(value, dict):
                load_name = value.get("LoadName", value.get("type", ""))
            else:
                load_name = str(value)
            if "tip" in load_name.lower():
                if not default_rack:
                    default_rack = load_name
                    
    # Check Requirements.Labware (pre-HAL format with type)
    if not default_rack and "Requirements" in recipe:
        req = recipe["Requirements"]
        if isinstance(req, dict) and "Labware" in req:
            for key, value in req["Labware"].items():
                if isinstance(value, dict):
                    load_name = value.get("type", value.get("LoadName", ""))
                else:
                    load_name = str(value)
                if "tip" in load_name.lower():
                    if not default_rack:
                        default_rack = load_name
                    
    if not default_rack:
        return usage
        
    # Calculate from steps
    for step in recipe.get("Steps", []):
        cmd = step.get("Command", "").lower()  # Normalize to lowercase
        tip_strategy = step.get("NewTip", "once")
        tips_to_add = 0
        
        if cmd in ["pickuptip", "pick_up_tip"]:
            tips_to_add = 1
        elif cmd in ["consumetips", "consume_tips"]:
            tips_to_add = step.get("Quantity", 1)
        elif cmd in ["transfer", "distribute", "consolidate", "mix"]:
            if tip_strategy == "never":
                tips_to_add = 0
            elif tip_strategy == "always":
                if cmd == "distribute":
                    dests = step.get("Destinations", [])
                    tips_to_add = len(dests) if isinstance(dests, list) else 1
                elif cmd == "consolidate":
                    sources = step.get("Sources", [])
                    tips_to_add = len(sources) if isinstance(sources, list) else 1
                else:
                    tips_to_add = 1
            else:  # "once"
                tips_to_add = 1
                
        if tips_to_add > 0:
            if default_rack not in usage:
                usage[default_rack] = 0
            usage[default_rack] += tips_to_add
            
    return usage


def count_racks_in_recipe(recipe: Dict[str, Any], rack_type: str) -> int:
    """
    Count how many racks of a type are in the recipe.
    
    Args:
        recipe: JSON recipe dictionary
        rack_type: Opentrons load name to count
        
    Returns:
        Number of racks
    """
    count = 0
    if "Labware" in recipe:
        for value in recipe["Labware"].values():
            if value.get("LoadName") == rack_type:
                count += 1
    return count
