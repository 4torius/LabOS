"""
Test script for the improved ProtocolGenerator with validation.
"""

import sys
import json
sys.path.insert(0, '.')

from protocol_generator import ProtocolGenerator, RecipeValidator

# Test recipe with errors
BAD_RECIPE = {
    "ProtocolName": "Test Validation",
    "Pipettes": {
        "left": "flex_1channel_1000"
    },
    "Labware": {
        "tips": {"LoadName": "opentrons_flex_96_tiprack_1000ul", "Slot": "B3"}
    },
    "Steps": [
        {"Command": "PickUpTip"},  # Missing PipetteMount
        {"Command": "Aspirate", "Volume": 100},  # Missing required fields
        {"Command": "UnknownCmd", "Foo": "bar"},  # Unknown command
        {"Command": "Dispense", "Volume": 100, "PipetteMount": "right", "Labware": "unknown:A1"}  # Wrong pipette
    ]
}

# Valid recipe
GOOD_RECIPE = {
    "ProtocolName": "Valid Protocol",
    "Pipettes": {
        "left": "flex_1channel_1000"
    },
    "Labware": {
        "tips": {"LoadName": "opentrons_flex_96_tiprack_1000ul", "Slot": "B3"},
        "plate": {"LoadName": "corning_96_wellplate_360ul_flat", "Slot": "B2"}
    },
    "TipRacks": {
        "left": ["tips"]
    },
    "Steps": [
        {"Command": "PickUpTip", "PipetteMount": "left"},
        {"Command": "Aspirate", "Volume": 100, "PipetteMount": "left", "Labware": "plate:A1"},
        {"Command": "Dispense", "Volume": 100, "PipetteMount": "left", "Labware": "plate:B1"},
        {"Command": "DropTip", "PipetteMount": "left"}
    ]
}

def test_validator():
    print("=" * 60)
    print("Testing RecipeValidator")
    print("=" * 60)
    
    validator = RecipeValidator()
    
    # Test bad recipe
    print("\n[Test 1] Bad recipe with errors:")
    is_valid, errors, warnings = validator.validate(BAD_RECIPE)
    print(f"  Valid: {is_valid}")
    print(f"  Errors ({len(errors)}):")
    for e in errors:
        print(f"    - Step {e.step_index}: [{e.command}] {e.field}: {e.message}")
    print(f"  Warnings ({len(warnings)}):")
    for w in warnings:
        print(f"    - Step {w.step_index}: [{w.command}] {w.field}: {w.message}")
    
    # Test good recipe
    print("\n[Test 2] Good recipe:")
    is_valid, errors, warnings = validator.validate(GOOD_RECIPE)
    print(f"  Valid: {is_valid}")
    print(f"  Errors: {len(errors)}")
    print(f"  Warnings: {len(warnings)}")
    
    return True

def test_generator():
    print("\n" + "=" * 60)
    print("Testing ProtocolGenerator")
    print("=" * 60)
    
    # Without simulation (faster)
    generator = ProtocolGenerator(temp_dir="./temp", enable_simulation=False)
    
    # Test bad recipe
    print("\n[Test 3] Generate from bad recipe:")
    result = generator.generate_and_validate(json.dumps(BAD_RECIPE))
    print(f"  Success: {result.success}")
    print(f"  Errors: {len(result.errors)}")
    print(f"  Warnings: {len(result.warnings)}")
    if result.filepath:
        print(f"  File: {result.filepath}")
    
    # Test good recipe
    print("\n[Test 4] Generate from good recipe:")
    result = generator.generate_and_validate(json.dumps(GOOD_RECIPE))
    print(f"  Success: {result.success}")
    print(f"  Errors: {len(result.errors)}")
    print(f"  Warnings: {len(result.warnings)}")
    if result.filepath:
        print(f"  File: {result.filepath}")
        # Cleanup
        generator.cleanup(result.filepath)
    
    # Test validate_only
    print("\n[Test 5] Validate only (no generation):")
    is_valid, errors, warnings = generator.validate_only(json.dumps(GOOD_RECIPE))
    print(f"  Valid: {is_valid}")
    
    return True

def test_json_error():
    print("\n" + "=" * 60)
    print("Testing JSON Error Handling")
    print("=" * 60)
    
    generator = ProtocolGenerator(enable_simulation=False)
    
    print("\n[Test 6] Invalid JSON:")
    result = generator.generate_and_validate("{ invalid json }")
    print(f"  Success: {result.success}")
    print(f"  Errors: {result.errors[0].message if result.errors else 'None'}")
    
    return True

if __name__ == "__main__":
    print("\n🧪 Protocol Generator Validation Tests\n")
    
    test_validator()
    test_generator()
    test_json_error()
    
    print("\n" + "=" * 60)
    print("✅ All tests completed!")
    print("=" * 60)
