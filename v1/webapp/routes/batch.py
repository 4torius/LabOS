"""Batch recipe/workflow/HAL generation API routes."""
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request


def create_batch_router(state, LIBRARY_DIR: Path) -> APIRouter:
    router = APIRouter(prefix="/api/batch", tags=["batch"])

    @router.post("/parse-chemical-excel")
    async def parse_chemical_excel(request: Request):
        """
        Parse an Excel file and return detected columns for user configuration.

        Step 1: Parse → return columns and data
        Step 2: User configures groups/delays in UI
        Step 3: Call /api/batch/generate-recipe-with-config
        """
        from src.excel_recipe_parser import (
            parse_excel_columns,
            generate_default_config,
            generate_recipe_with_config,
            save_recipe_to_library
        )
        try:
            content_type = request.headers.get("content-type", "")

            if "application/json" in content_type:
                data = await request.json()
                library_file = data.get("library_file")
                config = data.get("config")
                save_to_library = data.get("save_to_library", False)
                if not library_file:
                    return {"error": "No library file specified"}
                file_path = LIBRARY_DIR / "Recipes" / "excel" / library_file
                if not file_path.exists():
                    return {"error": f"File not found: {library_file}"}
                with open(file_path, "rb") as f:
                    content = f.read()
                filename = library_file
            else:
                form = await request.form()
                file = form.get("file")
                config = None
                save_to_library = form.get("save_to_library", "false").lower() == "true"
                if not file:
                    return {"error": "No file uploaded"}
                content = await file.read()
                filename = file.filename or "unknown.xlsx"

            result = parse_excel_columns(content, filename)
            if "error" in result:
                return result

            for sheet in result.get("sheets", []):
                if "error" not in sheet:
                    sheet["default_config"] = generate_default_config(sheet)

            if config:
                recipes = []
                for sheet in result.get("sheets", []):
                    if "error" not in sheet:
                        recipe = generate_recipe_with_config(sheet, config)
                        if "error" not in recipe:
                            recipes.append(recipe)
                            if save_to_library:
                                save_recipe_to_library(recipe, LIBRARY_DIR)
                result["recipes"] = recipes

            state.add_log("info", f"Parsed Excel: {filename}, {len(result.get('sheets', []))} sheets", "batch")
            return result

        except Exception as e:
            state.add_log("error", f"Excel parsing error: {str(e)}", "batch")
            return {"error": str(e)}

    @router.post("/generate-recipe-with-config")
    async def generate_recipe_with_config_endpoint(request: Request):
        """Generate a recipe from parsed sheet data using user-defined group/delay config."""
        from src.excel_recipe_parser import (
            generate_recipe_with_config,
            save_recipe_to_library,
            generate_hal_config_from_recipe,
            save_hal_config_to_library
        )
        try:
            data = await request.json()
            sheet_data = data.get("sheet_data")
            config = data.get("config")
            recipe_name = data.get("recipe_name")
            save_to_library = data.get("save_to_library", False)
            generate_hal = data.get("generate_hal", False)

            if not sheet_data:
                return {"error": "No sheet_data provided"}
            if not config:
                return {"error": "No config provided"}

            recipe = generate_recipe_with_config(sheet_data, config, recipe_name)
            if "error" in recipe:
                return recipe

            result: dict = {"recipe": recipe}
            if save_to_library:
                result["saved_recipe"] = save_recipe_to_library(recipe, LIBRARY_DIR)
            if generate_hal:
                hal_name = f"{recipe.get('ProtocolName', 'Recipe')}_HAL"
                hal_config = generate_hal_config_from_recipe(recipe, hal_name)
                result["hal_config"] = hal_config
                if save_to_library:
                    result["saved_hal"] = save_hal_config_to_library(hal_config, LIBRARY_DIR)

            state.add_log("info", f"Generated recipe: {recipe.get('ProtocolName')}", "batch")
            return result

        except Exception as e:
            state.add_log("error", f"Recipe generation error: {str(e)}", "batch")
            return {"error": str(e)}

    @router.get("/step-schema")
    async def get_step_schema():
        """Module-treatment catalog + presets for the recipe generator's '+ Add step' UI."""
        from src.excel_recipe_parser import get_step_type_catalog
        return get_step_type_catalog()

    @router.get("/library-excel-files")
    async def list_library_excel_files():
        """List Excel files in Library/Recipes/excel for chemical recipe import."""
        files = []
        excel_dir = LIBRARY_DIR / "Recipes" / "excel"
        if excel_dir.exists():
            for f in sorted(excel_dir.glob("*.xlsx")) + sorted(excel_dir.glob("*.xls")):
                files.append({
                    "filename": f.name,
                    "path": str(f.relative_to(LIBRARY_DIR)),
                    "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                    "type": "chemical"
                })
        return {"files": files, "directory": str(excel_dir)}

    @router.post("/generate-workflow")
    async def generate_workflow_from_recipes_api(request: Request):
        """Generate a workflow JSON from recipes with timing and extension points."""
        from src.excel_recipe_parser import generate_workflow_from_recipes, save_workflow_to_library
        try:
            data = await request.json()
            recipes = data.get("recipes", [])
            workflow_name = data.get("workflow_name")
            save_to_library = data.get("save_to_library", False)

            if not recipes:
                return {"error": "No recipes provided"}

            workflow = generate_workflow_from_recipes(recipes, workflow_name)
            saved_file = None
            if save_to_library:
                saved_file = save_workflow_to_library(workflow, LIBRARY_DIR)
                workflow["saved_file"] = saved_file

            state.add_log("info", f"Generated workflow '{workflow['name']}' with {len(workflow['steps'])} steps", "batch")
            return {"status": "success", "workflow": workflow, "saved_file": saved_file}

        except Exception as e:
            state.add_log("error", f"Workflow generation error: {str(e)}", "batch")
            return {"error": str(e)}

    @router.post("/generate-hal")
    async def generate_hal_config_api(request: Request):
        """Generate a generic HAL config from a recipe or explicit num_sources."""
        from src.excel_recipe_parser import generate_hal_config_from_recipe, generate_generic_hal, save_hal_config_to_library
        try:
            data = await request.json()
            recipe = data.get("recipe")
            hal_name = data.get("hal_name", "Generic_HAL")
            num_sources = data.get("num_sources")
            target_plate = data.get("target_plate", "corning_96_wellplate_360ul_flat")
            reservoir_type = data.get("reservoir_type", "nest_12_reservoir_15ml")
            save_to_library = data.get("save_to_library", False)
            tip_sizes_ul = data.get("tip_sizes_ul") or None

            if num_sources:
                hal_config = generate_generic_hal(num_sources=num_sources, hal_name=hal_name, target_plate=target_plate, reservoir_type=reservoir_type, tip_sizes_ul=tip_sizes_ul)
            elif recipe:
                hal_config = generate_hal_config_from_recipe(recipe, hal_name)
            else:
                hal_config = generate_generic_hal(num_sources=6, hal_name=hal_name, target_plate=target_plate, reservoir_type=reservoir_type, tip_sizes_ul=tip_sizes_ul)

            saved_file = None
            if save_to_library:
                saved_file = save_hal_config_to_library(hal_config, LIBRARY_DIR)
                hal_config["saved_file"] = saved_file

            state.add_log("info", f"Generated generic HAL '{hal_config.get('ConfigName')}' with {hal_config['Metadata']['total_sources']} sources", "batch")
            return {"status": "success", "hal_config": hal_config, "saved_file": saved_file}

        except Exception as e:
            state.add_log("error", f"HAL generation error: {str(e)}", "batch")
            return {"error": str(e)}

    return router
