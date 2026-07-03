#!/usr/bin/env python3
"""
Skill Validator - Quick validation for Claude Code skills
Validates YAML frontmatter, file structure, and naming conventions
"""

import os
import sys
import re
from pathlib import Path

def validate_skill(skill_path):
    """Validate a Claude Code skill directory"""
    errors = []
    warnings = []
    
    skill_path = Path(skill_path)
    
    # Check if SKILL.md exists
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        errors.append("Missing SKILL.md file")
        return errors, warnings
    
    # Read SKILL.md content
    content = skill_md.read_text(encoding='utf-8')
    
    # Extract YAML frontmatter
    if not content.startswith('---'):
        errors.append("SKILL.md must start with YAML frontmatter (---)")
        return errors, warnings
    
    # Find frontmatter boundaries
    frontmatter_match = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
    if not frontmatter_match:
        errors.append("Invalid YAML frontmatter format")
        return errors, warnings
    
    frontmatter = frontmatter_match.group(1)
    
    # Validate required fields
    name_match = re.search(r'^name:\s*(.+)$', frontmatter, re.MULTILINE)
    desc_match = re.search(r'^description:\s*(.+)$', frontmatter, re.MULTILINE)
    
    if not name_match:
        errors.append("Missing 'name' field in frontmatter")
    else:
        name = name_match.group(1).strip()
        # Validate name format
        if not re.match(r'^[a-z0-9-]+$', name):
            errors.append(f"Invalid name '{name}': must be lowercase letters, numbers, and hyphens only")
        if len(name) > 64:
            errors.append(f"Name '{name}' exceeds 64 characters")
        
        # Check if directory name matches skill name
        if skill_path.name != name:
            warnings.append(f"Directory name '{skill_path.name}' doesn't match skill name '{name}'")
    
    if not desc_match:
        errors.append("Missing 'description' field in frontmatter")
    else:
        description = desc_match.group(1).strip()
        if len(description) > 1024:
            errors.append(f"Description exceeds 1024 characters ({len(description)} chars)")
        if len(description) < 50:
            warnings.append(f"Description is very short ({len(description)} chars)")
        
        # Check for trigger terms
        trigger_keywords = ['use when', 'when you', 'for', 'to']
        has_triggers = any(keyword in description.lower() for keyword in trigger_keywords)
        if not has_triggers:
            warnings.append("Description should include 'when to use' trigger terms")
    
    # Check for version field
    version_match = re.search(r'^version:\s*(.+)$', frontmatter, re.MULTILINE)
    if not version_match:
        warnings.append("No 'version' field found (recommended)")
    
    # Check file structure
    if (skill_path / "REFERENCE.md").exists():
        ref_size = (skill_path / "REFERENCE.md").stat().st_size
        if ref_size > 100000:  # ~100KB
            warnings.append(f"REFERENCE.md is large ({ref_size} bytes). Consider splitting.")
    
    # Check if SKILL.md is too large
    skill_size = skill_md.stat().st_size
    if skill_size > 50000:  # ~50KB
        warnings.append(f"SKILL.md is large ({skill_size} bytes). Consider moving content to supporting files.")
    
    return errors, warnings


def main():
    if len(sys.argv) < 2:
        print("Usage: python validate_skill.py <path-to-skill-directory>")
        sys.exit(1)
    
    skill_path = sys.argv[1]
    
    if not os.path.isdir(skill_path):
        print(f"Error: '{skill_path}' is not a directory")
        sys.exit(1)
    
    print(f"Validating skill: {skill_path}")
    print("-" * 50)
    
    errors, warnings = validate_skill(skill_path)
    
    if errors:
        print(f"\n❌ ERRORS ({len(errors)}):")
        for error in errors:
            print(f"  • {error}")
    
    if warnings:
        print(f"\n⚠️  WARNINGS ({len(warnings)}):")
        for warning in warnings:
            print(f"  • {warning}")
    
    if not errors and not warnings:
        print("\n✅ Skill validation passed!")
        sys.exit(0)
    elif not errors:
        print(f"\n✅ Skill is valid (with {len(warnings)} warnings)")
        sys.exit(0)
    else:
        print(f"\n❌ Skill validation failed ({len(errors)} errors)")
        sys.exit(1)


if __name__ == "__main__":
    main()
