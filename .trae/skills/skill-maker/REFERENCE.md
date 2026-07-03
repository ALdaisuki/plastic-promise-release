# Skill Maker Reference

Complete reference guide for creating Claude Code skills.

## YAML Frontmatter Fields

### Required Fields

#### name
- **Type**: String
- **Required**: Yes
- **Max Length**: 64 characters
- **Format**: Lowercase letters, numbers, and hyphens only
- **Example**: `commit-helper`, `pdf-processing`, `code-reviewer`
- **Validation**: `^[a-z0-9-]+$` (regex pattern)
- **Notes**: Must match the directory name

#### description
- **Type**: String
- **Required**: Yes
- **Max Length**: 1024 characters
- **Format**: Natural language description
- **Purpose**: Claude uses this to decide when to activate the skill
- **Structure**: 
  - Sentence 1: What the skill does (list specific capabilities)
  - Sentence 2: When to use it (include trigger keywords)
- **Example**: 
  ```yaml
  description: Extract text and tables from PDF files, fill forms, merge documents. Use when working with PDF files or when the user mentions PDFs, forms, or document extraction.
  ```

### Optional Fields

#### allowed-tools
- **Type**: String (comma-separated) or Array
- **Required**: No
- **Purpose**: Restrict which tools Claude can use when this skill is active
- **Format Options**:
  ```yaml
  # Comma-separated string
  allowed-tools: Read, Grep, Glob
  
  # YAML array
  allowed-tools:
    - Read
    - Grep
    - Glob
  ```
- **Common Tool Sets**:
  - Read-only: `Read, Grep, Glob`
  - Python execution: `Read, Bash(python:*)`
  - File operations: `Read, Write, Create`
  - Git operations: `Read, Bash(git:*)`
- **Notes**: 
  - Only supported in Claude Code
  - If omitted, no tool restrictions apply
  - Tools listed don't require permission when skill is active

#### model
- **Type**: String
- **Required**: No
- **Purpose**: Specify which model to use when this skill is active
- **Format**: Model identifier string
- **Example**: `claude-sonnet-4-20250514`
- **Default**: Uses the conversation's current model
- **Use Cases**: Skills requiring specific model capabilities

#### context
- **Type**: String
- **Required**: No
- **Values**: `fork`
- **Purpose**: Run skill in isolated subagent context
- **Example**:
  ```yaml
  context: fork
  ```
- **Use Cases**:
  - Complex multi-step operations
  - When you don't want to clutter main conversation
  - Operations needing separate conversation history

#### agent
- **Type**: String
- **Required**: No (only with `context: fork`)
- **Purpose**: Specify which agent type to use in forked context
- **Values**: `Explore`, `Plan`, `general-purpose`, or custom agent name
- **Default**: `general-purpose`
- **Example**:
  ```yaml
  context: fork
  agent: general-purpose
  ```
- **Notes**: Only applicable when `context: fork` is set

#### hooks
- **Type**: Object
- **Required**: No
- **Purpose**: Define lifecycle hooks for the skill
- **Events**: `PreToolUse`, `PostToolUse`, `Stop`
- **Example**:
  ```yaml
  hooks:
    PreToolUse:
      - matcher: "Bash"
        hooks:
          - type: command
            command: "./scripts/security-check.sh $TOOL_INPUT"
            once: true
  ```
- **Options**:
  - `once: true` - Run hook only once per session
- **Use Cases**: Security checks, logging, validation

#### user-invocable
- **Type**: Boolean
- **Required**: No
- **Default**: `true`
- **Purpose**: Control whether skill appears in slash command menu
- **Values**: `true` or `false`
- **Example**:
  ```yaml
  user-invocable: false
  ```
- **Notes**:
  - `false` hides from `/` menu but Claude can still invoke it
  - Doesn't affect automatic discovery or Skill tool invocation
- **Use Cases**: Internal skills Claude uses but users don't need to invoke manually

#### disable-model-invocation
- **Type**: Boolean
- **Required**: No
- **Default**: `false`
- **Purpose**: Block programmatic invocation via Skill tool
- **Example**:
  ```yaml
  disable-model-invocation: true
  ```
- **Use Cases**: Skills that should only be manually invoked by users

## File Structure Conventions

### Directory Structure

```
skill-name/
├── SKILL.md              # Required - Main skill definition
├── REFERENCE.md          # Optional - Detailed API/reference docs
├── EXAMPLES.md           # Optional - Usage examples
├── FORMS.md              # Optional - Form mappings, data schemas
└── scripts/              # Optional - Utility scripts
    ├── helper.py
    ├── validate.py
    └── process.sh
```

### SKILL.md Structure

```markdown
---
{YAML frontmatter}
---

# {Skill Title}

## Overview
{Brief explanation of what this skill does}

## Instructions
{Clear, step-by-step guidance}

## Best Practices
{Guidelines to follow}

## Examples
{Concrete usage examples}

## Requirements
{Dependencies, packages, environment setup}

## Additional Resources
{Links to supporting files}
```

### Supporting File Guidelines

#### When to Create REFERENCE.md
- Complex APIs or data structures
- Detailed technical documentation
- Content that would make SKILL.md exceed 500 lines
- Comprehensive field mappings or schemas

#### When to Create EXAMPLES.md
- Multiple usage scenarios
- Code-heavy examples
- Before/after comparisons
- Different use case demonstrations

#### When to Create Scripts
- Validation logic verbose to describe
- Data processing better as tested code
- Operations needing consistency
- Complex transformations or calculations

#### Linking Supporting Files

Always link from SKILL.md to make Claude aware:

```markdown
## Additional Resources

For detailed API reference, see [REFERENCE.md](REFERENCE.md).
For usage examples, see [EXAMPLES.md](EXAMPLES.md).

## Utility Scripts

Run validation script:
```bash
python scripts/validate.py input.json
```
```

## Progressive Disclosure Patterns

### Pattern 1: Quick Start + Deep Reference

**SKILL.md** - Essential info and quick start:
```markdown
## Quick Start

{Minimal example to get started}

## Common Operations

{Most frequent use cases}

For complete API details, see [REFERENCE.md](REFERENCE.md).
```

**REFERENCE.md** - Comprehensive documentation:
```markdown
# Complete API Reference

## Functions

### function_name()
{Detailed documentation}
```

### Pattern 2: Overview + Domain-Specific Docs

**SKILL.md** - Overview and navigation:
```markdown
## Overview

{High-level explanation}

## Domains

- For form handling, see [FORMS.md](FORMS.md)
- For data validation, see [VALIDATION.md](VALIDATION.md)
```

### Pattern 3: Instructions + Examples

**SKILL.md** - Instructions only:
```markdown
## Instructions

{Step-by-step guide}

For practical examples, see [EXAMPLES.md](EXAMPLES.md).
```

**EXAMPLES.md** - Code examples:
```markdown
# Usage Examples

## Example 1: Basic Usage
{Code example}

## Example 2: Advanced Usage
{Code example}
```

## Description Writing Guide

### Anatomy of a Good Description

```
[What it does: specific capabilities] + [When to use: trigger terms and domains]
```

### Examples by Pattern

#### Data Processing Skills
```yaml
description: Extract, transform, and analyze CSV and JSON data files. Use when processing data, cleaning datasets, or when the user mentions data files, ETL, or data transformation.
```

#### Code Quality Skills
```yaml
description: Reviews pull requests for code quality, security vulnerabilities, and style consistency. Use when reviewing PRs, analyzing code changes, or when the user asks to review code.
```

#### Documentation Skills
```yaml
description: Generates comprehensive API documentation from code comments and type hints. Use when documenting code, creating API docs, or when the user asks to document functions.
```

#### DevOps Skills
```yaml
description: Creates and manages Docker containers, builds images, and configures multi-container applications. Use when working with Docker, containerization, or deployment workflows.
```

### Trigger Term Strategy

Include terms users naturally say:
- Action verbs: "review", "generate", "analyze", "process"
- Domain terms: "PDF", "database", "API", "test"
- Task descriptions: "commit message", "code quality", "data cleaning"
- Question patterns: "how does this work", "explain this code"

### Description Anti-Patterns

❌ **Too Vague**
```yaml
description: Helps with documents
```

❌ **No Trigger Terms**
```yaml
description: A utility for various tasks
```

❌ **Too Technical**
```yaml
description: Implements strategy pattern for polymorphic behavior
```

✅ **Good Balance**
```yaml
description: Extract text and metadata from PDF files using pypdf and pdfplumber. Use when working with PDF documents, extracting content, or when user mentions PDF files.
```

## Validation Rules

### Name Validation
```python
import re

def validate_name(name):
    if not name:
        return False, "Name cannot be empty"
    if len(name) > 64:
        return False, "Name exceeds 64 characters"
    if not re.match(r'^[a-z0-9-]+$', name):
        return False, "Name must contain only lowercase letters, numbers, and hyphens"
    return True, "Valid"
```

### Description Validation
```python
def validate_description(desc):
    if not desc:
        return False, "Description cannot be empty"
    if len(desc) > 1024:
        return False, "Description exceeds 1024 characters"
    if len(desc) < 20:
        return False, "Description too short (minimum 20 characters)"
    # Check for action words
    action_words = ['use when', 'for', 'with', 'analyze', 'process', 'generate', 'create', 'review']
    if not any(word in desc.lower() for word in action_words):
        return False, "Description should include trigger terms (use when, for, etc.)"
    return True, "Valid"
```

### YAML Validation
```python
def validate_yaml_structure(content):
    lines = content.split('\n')
    if not lines[0].strip() == '---':
        return False, "YAML frontmatter must start on line 1 with ---"
    
    # Find closing ---
    closing_index = -1
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == '---':
            closing_index = i
            break
    
    if closing_index == -1:
        return False, "YAML frontmatter must end with ---"
    
    # Check for tabs
    yaml_content = '\n'.join(lines[1:closing_index])
    if '\t' in yaml_content:
        return False, "YAML must use spaces, not tabs"
    
    return True, "Valid"
```

## Common Tool Patterns

### Read-Only Access
```yaml
allowed-tools: Read, Grep, Glob
```
**Use Cases**: Code analysis, documentation generation, read-only inspection

### Python Script Execution
```yaml
allowed-tools: Read, Bash(python:*)
```
**Use Cases**: Data processing, script execution, Python-based tasks

### File Manipulation
```yaml
allowed-tools: Read, Write, Create, Delete
```
**Use Cases**: File generation, content modification, file operations

### Git Operations
```yaml
allowed-tools: Read, Bash(git:*)
```
**Use Cases**: Git workflows, commit analysis, branch operations

### Database Access
```yaml
allowed-tools: Read, Bash(psql:*), Bash(mysql:*)
```
**Use Cases**: Database queries, schema inspection, data analysis

### No Restrictions
```yaml
# Omit allowed-tools field
```
**Use Cases**: General-purpose skills, when full tool access needed

## Skill Lifecycle

### 1. Discovery Phase
- Claude loads name and description at startup
- Descriptions cached for fast lookup
- Full SKILL.md not loaded yet

### 2. Activation Phase
- User request matches description
- Claude asks permission to use skill
- Full SKILL.md loaded into context
- Supporting files loaded if referenced

### 3. Execution Phase
- Claude follows instructions
- Loads referenced files as needed
- Executes scripts without reading them
- Applies tool restrictions if configured

### 4. Cleanup Phase
- Hooks with `once: true` removed
- Context returned to main conversation (unless forked)
- Skill remains available for reuse

## Best Practices Summary

### DO
- ✅ Keep SKILL.md under 500 lines
- ✅ Use progressive disclosure for complex skills
- ✅ Include specific trigger terms in descriptions
- ✅ Provide step-by-step instructions
- ✅ Link to supporting files from SKILL.md
- ✅ Use spaces for YAML indentation
- ✅ Test descriptions with natural language
- ✅ Include examples where helpful
- ✅ Document dependencies clearly
- ✅ Use allowed-tools for security-sensitive operations

### DON'T
- ❌ Use vague descriptions
- ❌ Exceed character limits (name: 64, description: 1024)
- ❌ Use tabs in YAML
- ❌ Put blank lines before first ---
- ❌ Deeply nest file references (A→B→C)
- ❌ Include entire reference docs in SKILL.md
- ❌ Use uppercase or underscores in names
- ❌ Forget to link supporting files
- ❌ Write descriptions without trigger terms
- ❌ Omit step-by-step instructions

## Troubleshooting

### Skill Not Loading
1. Check file path: `.claude/skills/{name}/SKILL.md`
2. Verify YAML starts on line 1 (no blank lines before ---)
3. Validate YAML syntax (no tabs, proper indentation)
4. Check name matches directory name
5. Verify name and description fields exist

### Skill Not Triggering
1. Add more specific trigger terms to description
2. Include keywords users naturally say
3. Test with requests that match description
4. Avoid vague or generic descriptions

### Supporting Files Not Loading
1. Verify files are linked from SKILL.md
2. Use relative paths: `[file](file.md)`, not `[file](./file.md)`
3. Keep references one level deep
4. Check file exists in skill directory

### Tool Restrictions Not Working
1. Verify `allowed-tools` format (comma-separated or YAML list)
2. Check tool names are correct
3. Confirm using Claude Code (not other Claude products)
4. Test that restricted tools require permission

## Version History

Based on Claude Code Skills documentation as of January 2026.
