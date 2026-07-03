---
name: skill-maker
description: Creates new Claude Code skills following official standards and best practices. Use when you need to generate a new skill, create skill documentation, or scaffold a skill structure. Automatically validates naming, descriptions, and ensures quality standards.
allowed-tools: Read, Write, Create, Grep, Glob
version: 1.0.0
---

# Skill Maker

Generate high-quality Claude Code skills that follow official standards and best practices.

## Overview

This skill helps you create new Claude Code skills by:
1. Gathering requirements through targeted questions
2. Validating all inputs against official standards
3. Generating properly structured SKILL.md files
4. Creating supporting documentation when needed
5. Ensuring consistency across all generated skills

## Instructions

### Step 1: Gather Requirements

When a user requests a new skill, ask these questions if not already provided:

**Required Information:**
1. **Skill Name**: What should the skill be called?
   - Must be lowercase letters, numbers, and hyphens only
   - Maximum 64 characters
   - Should be descriptive and clear
   - Example: `commit-helper`, `pdf-processing`, `code-reviewer`

2. **Purpose**: What specific task will this skill perform?
   - Be specific about capabilities
   - Avoid vague descriptions
   
3. **Trigger Terms**: What keywords or phrases should activate this skill?
   - Include natural language users would say
   - List specific actions or domains
   - Example: "when reviewing pull requests", "when working with PDFs"

**Optional But Recommended:**
4. **Tool Restrictions**: Should this skill be limited to specific tools?
   - Use `allowed-tools` for read-only or restricted operations
   - Examples: `Read, Grep, Glob` for read-only access
   - Leave unrestricted if not specified

5. **Supporting Files**: Does this skill need additional documentation?
   - Reference guides (REFERENCE.md)
   - Examples (EXAMPLES.md)
   - Utility scripts
   - Form mappings or data schemas

6. **Dependencies**: Does this skill require external packages?
   - Python packages (pip install)
   - Node packages (npm install)
   - System utilities

7. **Context**: Should this skill run in a forked context?
   - Use `context: fork` for complex multi-step operations
   - Use default context for most skills

### Step 2: Validate Inputs

Before generating the skill, validate:

**Name Validation:**
- Contains only lowercase letters, numbers, and hyphens
- No spaces, underscores, or special characters
- Maximum 64 characters
- Not empty

**Description Validation:**
- Maximum 1024 characters
- Includes what the skill does (specific capabilities)
- Includes when to use it (trigger terms)
- Not vague or generic
- Uses natural language users would say

**File Structure:**
- Directory name matches skill name
- SKILL.md exists at root of skill directory
- Supporting files referenced from SKILL.md
- Scripts in scripts/ subdirectory (if applicable)

### Step 3: Generate SKILL.md

Create the SKILL.md file with this structure:

```markdown
---
name: {skill-name}
description: {comprehensive-description-with-triggers}
{optional-fields}
---

# {Skill Title}

## Overview

{Brief explanation of what this skill does}

## Instructions

{Clear, step-by-step guidance for Claude}

1. {First step}
2. {Second step}
3. {Third step}

## Best Practices

{Important guidelines to follow}

## Examples

{Concrete usage examples if applicable}

{Additional sections as needed}
```

**Optional Frontmatter Fields to Include When Relevant:**

```yaml
allowed-tools: Tool1, Tool2, Tool3  # or YAML list format
model: claude-sonnet-4-20250514     # if specific model needed
context: fork                        # if needs isolated context
agent: general-purpose              # if using forked context
user-invocable: true                # default is true
```

### Step 4: Apply Best Practices

**Description Best Practices:**
- First sentence: What the skill does (list specific capabilities)
- Second sentence: When to use it (include trigger keywords)
- Include domain-specific terms users would mention
- ✅ Good: "Extract text and tables from PDF files, fill forms, merge documents. Use when working with PDF files or when the user mentions PDFs, forms, or document extraction."
- ❌ Bad: "Helps with documents"

**Instruction Best Practices:**
- Use numbered steps for sequential tasks
- Be specific and actionable
- Include code examples where helpful
- Mention error handling and edge cases
- Keep SKILL.md under 500 lines

**Progressive Disclosure:**
- Put essential info in SKILL.md
- Create separate files for detailed reference material:
  - `REFERENCE.md` for API documentation
  - `EXAMPLES.md` for usage examples
  - `scripts/` directory for utility scripts
- Link from SKILL.md to supporting files
- Tell Claude to execute scripts, not read them (saves context)

**Tool Restrictions:**
- Use `allowed-tools` for security-sensitive or read-only skills
- List tools as comma-separated string or YAML list
- Common tool sets:
  - Read-only: `Read, Grep, Glob`
  - Python execution: `Read, Bash(python:*)`
  - File operations: `Read, Write, Create`

### Step 5: Create Supporting Files (if needed)

**When to create REFERENCE.md:**
- Skill has complex APIs or data structures
- Detailed technical documentation needed
- Would exceed 500 lines in SKILL.md

**When to create EXAMPLES.md:**
- Multiple usage scenarios
- Code-heavy examples
- Before/after comparisons

**When to create scripts/:**
- Validation logic that's verbose to describe
- Data processing better as tested code
- Operations needing consistency across uses

**Script Integration:**
```markdown
Run the validation script to check inputs:
```bash
python scripts/validate.py input.json
```

Do not read the script - just execute it and use the output.


### Step 6: Generate Complete File Structure

Show the user the complete file structure that will be created:

```
skill-name/
├── SKILL.md              # Required
├── REFERENCE.md          # If needed
├── EXAMPLES.md           # If needed
└── scripts/              # If needed
    └── utility.py
```

### Step 7: Create Files

Generate and write all files to `.claude/skills/{skill-name}/` directory.

### Step 8: Verification

After creating the skill, verify:
1. ✅ SKILL.md has valid YAML frontmatter
2. ✅ Frontmatter starts on line 1 (no blank lines before ---)
3. ✅ Name and description are present and valid
4. ✅ Description includes what it does AND when to use it
5. ✅ Instructions are clear and actionable
6. ✅ Supporting files are linked from SKILL.md
7. ✅ Directory name matches skill name
8. ✅ Uses spaces for YAML indentation (not tabs)

**Optional: Run validation script** (if user wants deterministic check):
```bash
python .claude/skills/skill-maker/scripts/validate_skill.py .claude/skills/{skill-name}
```

This script checks YAML format, name conventions, description length, and file structure.

## Utility Scripts

The skill-maker includes optional Python scripts in `scripts/`:

### `init_skill.py` - Quick Skill Scaffold
Creates skill directory structure instantly:
```bash
python scripts/init_skill.py new-skill-name .claude/skills
```

**Use when:** User wants deterministic file creation without AI variability

### `validate_skill.py` - Validate Generated Skills
Checks frontmatter, naming, and structure:
```bash
python scripts/validate_skill.py .claude/skills/my-skill
```

**Use when:** User wants to verify a skill before using it

**Note:** These scripts are optional. skill-maker can generate skills without them (pure AI approach).

## Quality Checklist

Before finalizing a skill, ensure:

- [ ] Name is lowercase, hyphens only, max 64 chars
- [ ] Description is specific, includes triggers, max 1024 chars
- [ ] YAML frontmatter is valid (starts line 1, proper indentation)
- [ ] Instructions are clear, numbered, and actionable
- [ ] Code examples are included where helpful
- [ ] Best practices section exists
- [ ] Supporting files are linked (if applicable)
- [ ] Dependencies are documented (if any)
- [ ] SKILL.md is under 500 lines (or uses progressive disclosure)
- [ ] File structure follows conventions
- [ ] Tool restrictions are appropriate (if any)

## Common Patterns

### Simple Single-File Skill
```yaml
---
name: commit-helper
description: Generates clear commit messages from git diffs. Use when writing commit messages or reviewing staged changes.
---

# Instructions
1. Run `git diff --staged`
2. Analyze changes
3. Generate message with summary + details
```

### Multi-File Skill with Reference Docs
```yaml
---
name: pdf-processing
description: Extract text, fill forms, merge PDFs. Use when working with PDF files, forms, or document extraction.
allowed-tools: Read, Bash(python:*)
---

# Quick Start
{Essential instructions}

For detailed API reference, see [REFERENCE.md](REFERENCE.md).
For examples, see [EXAMPLES.md](EXAMPLES.md).
```

### Read-Only Skill
```yaml
---
name: code-analysis
description: Analyze code quality without making changes. Use for code review, quality checks, or static analysis.
allowed-tools: Read, Grep, Glob
---
```

### Forked Context Skill
```yaml
---
name: complex-refactoring
description: Perform multi-step refactoring operations. Use for large-scale code restructuring.
context: fork
agent: general-purpose
---
```

## Error Prevention

**Common Mistakes to Avoid:**

1. **Vague Descriptions**
   - ❌ "Helps with code"
   - ✅ "Reviews pull requests for code quality, security issues, and style consistency. Use when reviewing PRs or analyzing code changes."

2. **Invalid Names**
   - ❌ "My_Skill", "skill maker", "SKILL-1"
   - ✅ "my-skill", "skill-maker", "skill-1"

3. **Missing Frontmatter**
   - Must start on line 1
   - Must have opening and closing ---
   - Must include name and description

4. **Tab Indentation in YAML**
   - YAML requires spaces, not tabs
   - Use 2 spaces per indentation level

5. **Overly Long SKILL.md**
   - Keep under 500 lines
   - Use progressive disclosure for longer content
   - Create separate reference files

## Response Format

When generating a skill, provide:

1. **Summary** of what will be created
2. **File structure** showing all files
3. **Content** of each file
4. **Next steps** for testing the skill

Example response format:
```
I'll create the {skill-name} skill with the following structure:

{skill-name}/
├── SKILL.md
└── REFERENCE.md (if needed)

Creating files...
[Files created successfully]

To test this skill:
1. Ask Claude: "What Skills are available?"
2. Try a request that matches the trigger: "{example request}"
3. Verify the skill activates and follows its instructions
```

## Additional Resources

For detailed authoring guidance, refer to:
- Official Skills documentation structure
- YAML validation rules
- Progressive disclosure patterns
- Tool restriction patterns

## Notes

- Skills are automatically loaded when created or modified
- No restart needed - changes take effect immediately
- Skills can be tested by matching their description triggers
- Use `/skill-name` to manually invoke if needed
