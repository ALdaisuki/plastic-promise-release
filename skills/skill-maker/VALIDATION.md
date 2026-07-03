# Skill Quality Checklist

Use this checklist to validate skills before finalizing them.

## Pre-Generation Checklist

Before creating the skill, verify you have:

- [ ] Skill name (lowercase, hyphens only, max 64 chars)
- [ ] Clear purpose and capabilities
- [ ] Trigger terms users would naturally say
- [ ] Tool restrictions (if needed)
- [ ] Dependencies documented (if any)
- [ ] Supporting files identified (if needed)

## YAML Frontmatter Validation

### Required Fields
- [ ] `name` field exists
- [ ] Name is lowercase with hyphens only
- [ ] Name is 64 characters or less
- [ ] Name matches directory name
- [ ] `description` field exists
- [ ] Description is 1024 characters or less
- [ ] Description includes what the skill does
- [ ] Description includes when to use it (trigger terms)

### Optional Fields (if used)
- [ ] `allowed-tools` format is correct (string or array)
- [ ] Tool names are valid
- [ ] `model` string is valid model identifier (if used)
- [ ] `context: fork` is used appropriately (if used)
- [ ] `agent` is specified when using `context: fork` (if needed)
- [ ] `hooks` syntax is correct (if used)
- [ ] `user-invocable` is boolean (if used)

### YAML Syntax
- [ ] Frontmatter starts with `---` on line 1
- [ ] No blank lines before opening `---`
- [ ] Frontmatter ends with `---`
- [ ] Uses spaces for indentation (NO TABS)
- [ ] 2-space indentation for nested items
- [ ] All strings properly quoted (if containing special chars)
- [ ] YAML is valid and parseable

## Content Validation

### Instructions Section
- [ ] Instructions are clear and actionable
- [ ] Steps are numbered if sequential
- [ ] Code examples included where helpful
- [ ] Error handling mentioned
- [ ] Edge cases addressed
- [ ] Language is conversational and clear

### Structure
- [ ] Has clear heading hierarchy
- [ ] Sections are logically organized
- [ ] Overview section explains purpose
- [ ] Instructions section provides steps
- [ ] Best practices section included (if applicable)
- [ ] Examples section included (if helpful)
- [ ] Requirements section lists dependencies (if any)

### Length
- [ ] SKILL.md is under 500 lines
- [ ] If over 500 lines, uses progressive disclosure
- [ ] Supporting files created for detailed content
- [ ] Links to supporting files from SKILL.md

## Description Quality Check

### Specificity
- [ ] Describes specific capabilities (not vague)
- [ ] Lists concrete actions the skill performs
- [ ] Avoids generic terms like "helps with" or "handles"

### Trigger Terms
- [ ] Includes "Use when" or similar phrase
- [ ] Contains keywords users would say
- [ ] Includes domain-specific terms
- [ ] Has action verbs (extract, analyze, generate, etc.)

### Examples of Good Descriptions
- ✅ "Extract text and tables from PDF files, fill forms, merge documents. Use when working with PDF files or when the user mentions PDFs, forms, or document extraction."
- ✅ "Reviews pull requests for code quality, security vulnerabilities, and style consistency. Use when reviewing PRs, analyzing code changes, or when the user asks to review code."
- ✅ "Generates clear commit messages from git diffs. Use when writing commit messages or reviewing staged changes."

### Examples of Bad Descriptions
- ❌ "Helps with documents"
- ❌ "A utility for code"
- ❌ "Performs various tasks"

## File Structure Validation

### Directory Structure
- [ ] Directory name matches skill name exactly
- [ ] Directory is in `.claude/skills/`
- [ ] SKILL.md exists in skill directory root
- [ ] Supporting files are in skill directory (if any)
- [ ] Scripts are in `scripts/` subdirectory (if any)

### Supporting Files (if applicable)
- [ ] REFERENCE.md linked from SKILL.md (if exists)
- [ ] EXAMPLES.md linked from SKILL.md (if exists)
- [ ] Scripts referenced in instructions (if exist)
- [ ] Script execution instructions clear (not "read the script")
- [ ] File paths use forward slashes
- [ ] References are one level deep (not A→B→C)

## Tool Restrictions Validation (if used)

- [ ] `allowed-tools` field is present
- [ ] Tool names are valid and recognized
- [ ] Format is correct (comma-separated string OR YAML array)
- [ ] Tool set matches skill purpose (e.g., read-only for analysis)
- [ ] No contradictory tools (e.g., Read + Delete for read-only skill)

### Common Tool Sets
- Read-only: `Read, Grep, Glob`
- Python: `Read, Bash(python:*)`
- File ops: `Read, Write, Create`
- Git: `Read, Bash(git:*)`

## Dependencies Validation (if applicable)

- [ ] All required packages listed
- [ ] Installation commands provided
- [ ] Python packages: `pip install package-name`
- [ ] Node packages: `npm install package-name`
- [ ] System utilities documented
- [ ] Version requirements specified (if critical)

## Progressive Disclosure Validation (if used)

- [ ] Essential info in SKILL.md (overview, quick start)
- [ ] Detailed content in supporting files
- [ ] Supporting files linked from SKILL.md
- [ ] Link descriptions explain what's in each file
- [ ] Files are discoverable by Claude
- [ ] One level of reference depth maintained

## Script Integration Validation (if applicable)

- [ ] Scripts in `scripts/` directory
- [ ] Execution instructions clear (not reading instructions)
- [ ] Script purpose explained in SKILL.md
- [ ] Example execution commands provided
- [ ] Script file permissions mentioned if needed
- [ ] Output handling explained

### Example Script Integration
```markdown
✅ Good:
Run the validation script to check inputs:
```bash
python scripts/validate.py input.json
```
The script outputs errors if validation fails.

❌ Bad:
Read scripts/validate.py to understand validation logic.
```

## Context and Agent Validation (if used)

### For `context: fork`
- [ ] Used for complex multi-step operations
- [ ] Appropriate for task isolation
- [ ] `agent` field specified (if custom agent needed)
- [ ] Agent type is valid (Explore, Plan, general-purpose, or custom)

## Hooks Validation (if used)

- [ ] Hook event is valid (PreToolUse, PostToolUse, Stop)
- [ ] Matcher pattern is correct
- [ ] Command or script path is valid
- [ ] `once: true` used appropriately
- [ ] Hook purpose is clear

## Testing Checklist

### Pre-Deployment Testing
- [ ] Asked Claude "What Skills are available?"
- [ ] Skill appears in the list
- [ ] Description is displayed correctly
- [ ] Manual invocation works: `/skill-name`

### Trigger Testing
- [ ] Created test request matching description
- [ ] Skill activates automatically
- [ ] Claude follows instructions correctly
- [ ] No errors during execution

### Tool Restriction Testing (if applicable)
- [ ] Restricted tools require permission
- [ ] Allowed tools work without permission
- [ ] Behavior matches expectations

### Supporting Files Testing (if applicable)
- [ ] Claude loads referenced files when needed
- [ ] Scripts execute without reading
- [ ] Links work correctly
- [ ] Progressive disclosure works as intended

## Final Quality Review

### Consistency
- [ ] Terminology consistent throughout
- [ ] Formatting consistent
- [ ] Code style consistent
- [ ] Instructions follow same pattern

### Completeness
- [ ] All sections filled out
- [ ] No placeholder text remaining
- [ ] Examples provided where needed
- [ ] Edge cases covered

### Clarity
- [ ] Language is clear and direct
- [ ] No ambiguous instructions
- [ ] Technical terms explained
- [ ] Assumptions stated

### Usability
- [ ] Easy to understand at a glance
- [ ] Instructions are actionable
- [ ] Examples are practical
- [ ] Common use cases covered

## Common Issues and Fixes

### Issue: Skill doesn't load
**Check:**
- File path correct (`.claude/skills/name/SKILL.md`)
- YAML starts on line 1
- No tabs in YAML
- Valid YAML syntax

### Issue: Skill doesn't trigger
**Fix:**
- Add specific trigger terms to description
- Include keywords users naturally say
- Test with requests matching description

### Issue: Supporting files not loading
**Fix:**
- Link files from SKILL.md
- Use relative paths: `[file](file.md)`
- Verify files exist in skill directory

### Issue: Tool restrictions not working
**Check:**
- Format: `allowed-tools: Tool1, Tool2`
- Tool names are correct
- Using Claude Code (not other products)

### Issue: YAML parsing errors
**Fix:**
- Remove tabs, use spaces
- Check indentation (2 spaces per level)
- Ensure `---` delimiters present
- Validate quotes around special characters

### Issue: Description too vague
**Fix:**
- List specific capabilities
- Add "Use when" clause
- Include domain keywords
- Add action verbs

### Issue: SKILL.md too long
**Fix:**
- Move detailed docs to REFERENCE.md
- Move examples to EXAMPLES.md
- Keep essential info in SKILL.md
- Use progressive disclosure

## Sign-Off Checklist

Before marking skill as complete:

- [ ] All validation checks passed
- [ ] Tested successfully
- [ ] Documentation complete
- [ ] No known issues
- [ ] Ready for production use

## Quality Score

Rate each category (1-5, 5 is best):

- [ ] Name quality: ___/5
- [ ] Description quality: ___/5
- [ ] YAML correctness: ___/5
- [ ] Instruction clarity: ___/5
- [ ] File structure: ___/5
- [ ] Examples quality: ___/5
- [ ] Documentation completeness: ___/5
- [ ] Overall usability: ___/5

**Total Score: ___/40**

- 35-40: Excellent, ready to ship
- 28-34: Good, minor improvements needed
- 20-27: Fair, significant improvements needed
- Below 20: Needs major rework

## Notes Section

Additional observations or special considerations:

```
[Add any notes here]
```

---

**Validated by:** ___________
**Date:** ___________
**Status:** [ ] Approved [ ] Needs revision [ ] Rejected
