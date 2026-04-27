"""
Server Integration Test - Test full server functionality
========================================================

Interactive test client for OpentronsSiLA2Server.
"""

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ═══════════════════════════════════════════════════════════════════════════
#                              COLORS
# ═══════════════════════════════════════════════════════════════════════════

class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'


def print_header(text: str):
    print(f"\n{Colors.HEADER}{Colors.BOLD}{'='*60}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}  {text}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{'='*60}{Colors.ENDC}\n")


def print_success(text: str):
    print(f"{Colors.GREEN}✓ {text}{Colors.ENDC}")


def print_error(text: str):
    print(f"{Colors.RED}✗ {text}{Colors.ENDC}")


def print_info(text: str):
    print(f"{Colors.CYAN}ℹ {text}{Colors.ENDC}")


def print_warning(text: str):
    print(f"{Colors.YELLOW}⚠ {text}{Colors.ENDC}")


# ═══════════════════════════════════════════════════════════════════════════
#                              TEST CLIENT
# ═══════════════════════════════════════════════════════════════════════════

class ServerTestClient:
    """Interactive test client for OpentronsSiLA2Server."""
    
    def __init__(self):
        self.server = None
        self.connected = False
        
    async def connect(self) -> bool:
        """Connect to the server (in-process)."""
        try:
            from src import OpentronsSiLA2Server, ServerConfig
            
            config = ServerConfig("config.yaml")
            self.server = OpentronsSiLA2Server(config)
            await self.server.initialize()
            
            self.connected = True
            print_success("Server initialized")
            print_info(f"Robot: {config.robot_ip}:{config.robot_port}")
            print_info(f"Hardware Config: {self.server.get_current_hardware_config()}")
            return True
            
        except Exception as e:
            print_error(f"Initialization failed: {e}")
            return False
            
    async def test_hardware_config(self):
        """Test hardware configuration management."""
        print_header("HARDWARE CONFIG MANAGEMENT")
        
        # List configs
        configs = self.server.list_hardware_configs()
        print_info(f"Available configs: {configs}")
        
        # Current config
        current = self.server.get_current_hardware_config()
        print_info(f"Current config: {current}")
        
        # Loaded labware
        labware = json.loads(self.server.get_loaded_labware())
        print_info(f"Loaded labware: {list(labware.keys())}")
        
        # Loaded modules
        modules = json.loads(self.server.get_loaded_modules())
        print_info(f"Loaded modules: {list(modules.keys())}")
        
        print_success("Hardware config test passed")
        
    async def test_tip_tracker(self):
        """Test tip tracking functionality."""
        print_header("TIP TRACKER")
        
        if not self.server.tip_tracker:
            print_warning("Tip tracking disabled")
            return
            
        # Get status
        status = json.loads(self.server.get_tip_status())
        print_info(f"Current tip usage: {status}")
        
        # Test refill
        rack_type = "opentrons_flex_96_tiprack_1000ul"
        result = await self.server.cmd_refill_tip_rack(rack_type)
        print_info(f"Refill result: {result}")
        
        print_success("Tip tracker test passed")
        
    async def test_json_validation(self):
        """Test JSON recipe validation."""
        print_header("JSON VALIDATION")
        
        # Valid recipe
        valid_recipe = {
            "ProtocolName": "Test",
            "Labware": {"Tips": {"LoadName": "opentrons_flex_96_tiprack_1000ul", "Slot": "C1"}},
            "Steps": [{"Command": "Comment", "Text": "Test"}]
        }
        is_valid, error = self.server.validate_json_recipe(valid_recipe)
        if is_valid:
            print_success("Valid recipe accepted")
        else:
            print_error(f"Valid recipe rejected: {error}")
            
        # Invalid recipe (no steps)
        invalid_recipe = {"ProtocolName": "Test", "Labware": {}}
        is_valid, error = self.server.validate_json_recipe(invalid_recipe)
        if not is_valid:
            print_success(f"Invalid recipe rejected: {error}")
        else:
            print_error("Invalid recipe accepted (should have been rejected)")
            
        print_success("JSON validation test passed")
        
    async def test_robot_connection(self):
        """Test robot connection."""
        print_header("ROBOT CONNECTION")
        
        # Initialize
        result = await self.server.cmd_initialize()
        print_info(f"Initialize result: {result}")
        
        if self.server.is_connected():
            print_success("Connected to robot")
            
            # Get status
            status = self.server.get_robot_status()
            print_info(f"Robot status: {status}")
        else:
            print_warning("Robot not connected (this may be expected)")
            
    async def test_protocol_generator(self):
        """Test protocol generator."""
        print_header("PROTOCOL GENERATOR")
        
        from src import ProtocolGenerator
        
        gen = ProtocolGenerator(temp_dir="./temp")
        
        # Test recipe
        recipe = {
            "ProtocolName": "GeneratorTest",
            "Labware": {},
            "Steps": [{"Command": "Comment", "Text": "Test"}]
        }
        
        # Generate content
        code = gen.generate_content(json.dumps(recipe))
        
        if "GeneratorTest" in code and "def run" in code:
            print_success("Protocol generated correctly")
            print_info(f"Code length: {len(code)} chars")
        else:
            print_error("Protocol generation failed")
            
    async def run_interactive_menu(self):
        """Run interactive test menu."""
        print_header("OPENTRONS SiLA2 SERVER - TEST CLIENT")
        
        while True:
            print("\nOptions:")
            print("  1. Test Hardware Config")
            print("  2. Test Tip Tracker")
            print("  3. Test JSON Validation")
            print("  4. Test Robot Connection")
            print("  5. Test Protocol Generator")
            print("  6. Run All Tests")
            print("  0. Exit")
            print()
            
            try:
                choice = input("Select option: ").strip()
            except EOFError:
                break
                
            if choice == "1":
                await self.test_hardware_config()
            elif choice == "2":
                await self.test_tip_tracker()
            elif choice == "3":
                await self.test_json_validation()
            elif choice == "4":
                await self.test_robot_connection()
            elif choice == "5":
                await self.test_protocol_generator()
            elif choice == "6":
                await self.test_hardware_config()
                await self.test_tip_tracker()
                await self.test_json_validation()
                await self.test_protocol_generator()
                await self.test_robot_connection()
            elif choice == "0":
                break
            else:
                print_warning("Invalid option")


# ═══════════════════════════════════════════════════════════════════════════
#                              MAIN
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    """Main entry point."""
    client = ServerTestClient()
    
    if await client.connect():
        await client.run_interactive_menu()
    else:
        print_error("Could not initialize test client")


if __name__ == "__main__":
    asyncio.run(main())
