#!/usr/bin/env python3
"""
Init Skill - Creates a new Claude Code skill scaffold
Generates directory structure with SKILL.md template
"""

import os
import sys
from pathlib import Path

SKILL_TEMPLATE = '''---
name: {name}
description: [TODO: Add comprehensive description with trigger terms. Include what the skill does AND when to use it.]
allowed-tools: Read, Write, Create, Grep, Glob
version: 1.0.0
---

# {title}

## Overview

[TODO: Brief explanation of what this skill does]

## Instructions

### Step 1: [First Step Name]

[TODO: Describe the first step]

### Step 2: [Second Step Name]

[TODO: Describe the second step]

## Validation

Before completing, verify:
- [ ] [Validation criterion 1]
- [ ] [Validation criterion 2]
- [ ] [Validation criterion 3]

## Output

This skill generates:
- [Output file 1]
- [Output file 2]
'''

REFERENCE_TEMPLATE = '''# {title} - Reference Guide

## Detailed Documentation

[TODO: Add detailed reference information that Claude should load when needed]

## Common Patterns

[TODO: Add common usage patterns]

## Troubleshooting

[TODO: Add troubleshooting guide]
'''

EXAMPLES_TEMPLATE = '''# {title} - Examples

## Example 1: [Use Case Name]

**User Request**: "[Example user request]"

**Skill Output**:
```
[Show actual generated output]
```

**Outcome**: [Describe the result]

---

## Example 2: [Another Use Case]

**User Request**: "[Another example]"

**Skill Output**:
```
[Show actual output]
```

**Outcome**: [Describe the result]
'''

VALIDATION_TEMPLATE = '''# {title} - Validation Checklist

## Quality Checklist

Before marking the skill complete, verify:

### Core Requirements
- [ ] YAML frontmatter is valid
- [ ] Name follows format: lowercase-hyphens-only
- [ ] Description < 1024 chars with trigger terms
- [ ] All required files exist

### Content Quality
- [ ] Instructions are clear and actionable
- [ ] Steps are numbered and sequential
- [ ] Examples show actual outputs (not descriptions)
- [ ] No placeholder text remains

### Best Practices
- [ ] Token-efficient (no unnecessary verbosity)
- [ ] Progressive disclosure used appropriately
- [ ] References point to actual files
- [ ] Validation criteria are specific

## Common Issues

- **Issue**: [Common problem]
  - **Solution**: [How to fix]
'''


def create_skill_scaffold(skill_name, output_dir="."):
    """Create a new skill directory with template files"""
    
    # Validate skill name
    if not skill_name.islower() or not all(c.isalnum() or c == '-' for c in skill_name):
        print(f"Error: Skill name must be lowercase letters, numbers, and hyphens only")
        sys.exit(1)
    
    if len(skill_name) > 64:
        print(f"Error: Skill name must be 64 characters or less")
        sys.exit(1)
    
    # Create directory structure
    skill_dir = Path(output_dir) / skill_name
    
    if skill_dir.exists():
        print(f"Error: Directory '{skill_dir}' already exists")
        sys.exit(1)
    
    skill_dir.mkdir(parents=True)
    (skill_dir / "scripts").mkdir()
    (skill_dir / "references").mkdir(exist_ok=True)
    
    # Create title from name
    title = skill_name.replace('-', ' ').title()
    
    # Write template files
    (skill_dir / "SKILL.md").write_text(
        SKILL_TEMPLATE.format(name=skill_name, title=title),
        encoding='utf-8'
    )
    
    (skill_dir / "REFERENCE.md").write_text(
        REFERENCE_TEMPLATE.format(title=title),
        encoding='utf-8'
    )
    
    (skill_dir / "EXAMPLES.md").write_text(
        EXAMPLES_TEMPLATE.format(title=title),
        encoding='utf-8'
    )
    
    (skill_dir / "VALIDATION.md").write_text(
        VALIDATION_TEMPLATE.format(title=title),
        encoding='utf-8'
    )
    
    # Create placeholder script
    (skill_dir / "scripts" / "README.md").write_text(
        "# Scripts\n\nAdd executable scripts here (Python, Bash, etc.)\n",
        encoding='utf-8'
    )
    
    # Create LICENSE.txt
    (skill_dir / "LICENSE.txt").write_text(
        \"\"\"Apache License 2.0\n\nCopyright 2026\n\nLicensed under the Apache License, Version 2.0 (the \"License\");\nyou may not use this file except in compliance with the License.\nYou may obtain a copy of the License at\n\n    http://www.apache.org/licenses/LICENSE-2.0\n\nUnless required by applicable law or agreed to in writing, software\ndistributed under the License is distributed on an \"AS IS\" BASIS,\nWITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.\nSee the License for the specific language governing permissions and\nlimitations under the License.\n\nSee full license terms at: http://www.apache.org/licenses/LICENSE-2.0\n\"\"\",
        encoding='utf-8'
    )
    
    print(f"✅ Created skill scaffold: {skill_dir}")
    print(f"\nNext steps:")
    print(f"1. Edit {skill_dir}/SKILL.md (replace [TODO] sections)")
    print(f"2. Update description with trigger terms")
    print(f"3. Add examples in EXAMPLES.md")
    print(f"4. Test the skill in Claude Code")


def main():
    if len(sys.argv) < 2:
        print("Usage: python init_skill.py <skill-name> [output-directory]")
        print("\nExample: python init_skill.py my-new-skill ./skills")
        sys.exit(1)
    
    skill_name = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "."
    
    create_skill_scaffold(skill_name, output_dir)


if __name__ == "__main__":
    main()
