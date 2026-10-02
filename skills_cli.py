import argparse
import sys
from pathlib import Path
from skills import SkillRegistry, load_skill, SkillValidationError, DEFAULT_SKILLS_DIR
import json

def list_skills(args):
    registry = SkillRegistry()
    print("Installed Skills:")
    print(f"{'NAME':<20} {'ENABLED':<10} {'DESCRIPTION'}")
    print("-" * 60)
    for path in sorted(item for item in registry.root.iterdir() if item.is_dir()):
        skill_json = path / 'skill.json'
        if not skill_json.exists():
            continue
        try:
            skill = load_skill(path)
            is_enabled = not (path / '.disabled').exists()
            print(f"{skill.name:<20} {str(is_enabled):<10} {skill.description}")
        except SkillValidationError as e:
            print(f"{path.name:<20} {'ERROR':<10} {str(e)}")

def inspect_skill(args):
    registry = SkillRegistry()
    path = registry.root / args.name
    if not path.exists():
        print(f"Error: Skill '{args.name}' not found.")
        sys.exit(1)
    try:
        skill = load_skill(path)
        is_enabled = not (path / '.disabled').exists()
        print(f"Name: {skill.name}")
        print(f"Status: {'Enabled' if is_enabled else 'Disabled'}")
        print(f"Description: {skill.description}")
        print(f"Triggers: {', '.join(skill.triggers)}")
        print(f"Allowed Tools: {', '.join(skill.allowed_tools)}")
        print(f"Inputs: {json.dumps(skill.inputs)}")
        print(f"Execution: {json.dumps(skill.execution)}")
    except SkillValidationError as e:
        print(f"Error validating skill: {e}")
        sys.exit(1)

def enable_skill(args):
    registry = SkillRegistry()
    path = registry.root / args.name
    if not path.exists():
        print(f"Error: Skill '{args.name}' not found.")
        sys.exit(1)
    disabled_file = path / '.disabled'
    if disabled_file.exists():
        disabled_file.unlink()
        print(f"Skill '{args.name}' enabled.")
    else:
        print(f"Skill '{args.name}' is already enabled.")

def disable_skill(args):
    registry = SkillRegistry()
    path = registry.root / args.name
    if not path.exists():
        print(f"Error: Skill '{args.name}' not found.")
        sys.exit(1)
    disabled_file = path / '.disabled'
    if not disabled_file.exists():
        disabled_file.touch()
        print(f"Skill '{args.name}' disabled.")
    else:
        print(f"Skill '{args.name}' is already disabled.")

def validate_skill(args):
    registry = SkillRegistry()
    path = registry.root / args.name
    if not path.exists():
        print(f"Error: Skill '{args.name}' not found.")
        sys.exit(1)
    try:
        skill = load_skill(path)
        print(f"Skill '{args.name}' is valid.")
    except SkillValidationError as e:
        print(f"Skill '{args.name}' validation failed: {e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Skill Management CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    subparsers.add_parser("list", help="List all skills")
    
    parser_inspect = subparsers.add_parser("inspect", help="Inspect a skill")
    parser_inspect.add_argument("name", help="Name of the skill")
    
    parser_enable = subparsers.add_parser("enable", help="Enable a skill")
    parser_enable.add_argument("name", help="Name of the skill")
    
    parser_disable = subparsers.add_parser("disable", help="Disable a skill")
    parser_disable.add_argument("name", help="Name of the skill")
    
    parser_validate = subparsers.add_parser("validate", help="Validate a skill")
    parser_validate.add_argument("name", help="Name of the skill")
    
    args = parser.parse_args()
    
    if args.command == "list":
        list_skills(args)
    elif args.command == "inspect":
        inspect_skill(args)
    elif args.command == "enable":
        enable_skill(args)
    elif args.command == "disable":
        disable_skill(args)
    elif args.command == "validate":
        validate_skill(args)

if __name__ == "__main__":
    main()