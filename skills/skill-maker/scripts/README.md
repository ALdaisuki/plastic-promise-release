# Scripts for skill-maker

This directory contains utility scripts for skill creation and validation.

## Available Scripts

### 1. `init_skill.py` - Create New Skill Scaffold

Creates a complete skill directory structure with template files.

**Usage:**
```bash
python init_skill.py <skill-name> [output-directory]
```

**Example:**
```bash
python init_skill.py pdf-processor .claude/skills
```

**Creates:**
```
pdf-processor/
├── SKILL.md (with frontmatter template)
├── REFERENCE.md
├── EXAMPLES.md
├── VALIDATION.md
├── LICENSE.txt
└── scripts/
    └── README.md
```

**Validates:**
- Skill name is lowercase-hyphens only
- Name is ≤ 64 characters
- Directory doesn't already exist

---

### 2. `validate_skill.py` - Validate Existing Skill

Quick validation of skill structure and frontmatter.

**Usage:**
```bash
python validate_skill.py <path-to-skill-directory>
```

**Example:**
```bash
python validate_skill.py .claude/skills/my-skill
```

**Checks:**
- ✅ SKILL.md exists
- ✅ YAML frontmatter format
- ✅ Required fields (name, description)
- ✅ Name format (lowercase-hyphens)
- ✅ Description length (< 1024 chars)
- ⚠️ File sizes (warns if too large)
- ⚠️ Version field presence
- ⚠️ Trigger terms in description

**Exit codes:**
- `0` - Valid (may have warnings)
- `1` - Invalid (has errors)

---

## Requirements

**Python 3.6+** (no external dependencies)

Both scripts use only Python standard library:
- `os`, `sys`, `pathlib` for file operations
- `re` for regex validation

---

## Integration with skill-maker

These scripts are **optional utilities**. The skill-maker skill can:
1. Generate skills without these scripts (pure AI)
2. Reference these scripts for users who want deterministic validation
3. Suggest running `validate_skill.py` after generation

**Trade-off:**
- **With scripts:** Deterministic, instant validation
- **Without scripts:** Pure AI, no Python dependency

---

## Testing

**Test init_skill.py:**
```bash
# Create test skill
python init_skill.py test-skill ./test-output

# Verify structure
ls -la ./test-output/test-skill/
```

**Test validate_skill.py:**
```bash
# Validate an existing skill
python validate_skill.py ../skill-maker

# Should output: ✅ Skill validation passed!
```

---

## Future Enhancements

Potential additions:
- `package_skill.py` - Create .skill zip files
- `test_skill.py` - Run skill in test environment
- `sync_skill.py` - Sync to personal skills directory (~/.claude/skills/)

---

**Note:** These scripts match Anthropic's approach but are simplified for our pure-AI workflow.
