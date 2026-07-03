# Skill Maker Examples

Real-world examples of skills created with the skill-maker.

## Example 1: Simple Single-File Skill

### User Request
> "Create a skill that generates commit messages from git diffs"

### Questions Asked
1. Name: `commit-helper`
2. Tool restrictions: Read-only access needed? No, needs git access
3. Dependencies: Uses git (already available)

### Generated Skill

**File Structure:**
```
commit-helper/
└── SKILL.md
```

**commit-helper/SKILL.md:**
```markdown
---
name: commit-helper
description: Generates clear commit messages from git diffs. Use when writing commit messages, committing changes, or reviewing staged changes.
---

# Commit Helper

## Overview

This skill analyzes staged git changes and generates clear, descriptive commit messages following best practices.

## Instructions

1. **Check staged changes**
   ```bash
   git diff --staged
   ```

2. **Analyze the changes**
   - Identify modified files and their purposes
   - Understand what changed and why
   - Note any breaking changes or important updates

3. **Generate commit message with this structure:**
   - **Summary line**: Present tense, under 50 characters
   - **Blank line**
   - **Detailed description**: What and why (not how)
   - **List affected components** if multiple areas changed

4. **Format:**
   ```
   Add user authentication to API endpoints
   
   Implemented JWT-based authentication for /api/users routes.
   Added middleware to verify tokens on protected endpoints.
   
   Components:
   - API routes: user authentication
   - Middleware: token verification
   - Tests: auth flow coverage
   ```

## Best Practices

- Use present tense ("Add feature" not "Added feature")
- Start with verb (Add, Fix, Update, Remove, Refactor)
- Explain what and why, not how
- Keep summary under 50 characters
- Wrap body at 72 characters
- Reference issue numbers if applicable

## Common Verbs

- **Add**: New feature or functionality
- **Fix**: Bug fix
- **Update**: Modify existing feature
- **Remove**: Delete code or feature
- **Refactor**: Code restructuring without behavior change
- **Docs**: Documentation changes
- **Style**: Formatting, whitespace
- **Test**: Add or update tests
- **Chore**: Build process, dependencies


### Validation Results

✅ Name valid (lowercase, hyphens, <64 chars) \
✅ Description includes capabilities and triggers \
✅ Instructions clear and numbered \
✅ Examples provided \
✅ Best practices included \
✅ Under 500 lines

---

## Example 2: Multi-File Skill with Reference Docs

### User Request
> "Create a skill for processing PDF files - extracting text, filling forms, merging documents"

### Questions Asked
1. Name: `pdf-processing`
2. Dependencies: Requires pypdf and pdfplumber
3. Tool restrictions: Should only read and execute Python scripts
4. Supporting files: Detailed API reference needed? Yes

### Generated Skill

**File Structure:**
```
pdf-processing/
├── SKILL.md
├── REFERENCE.md
└── scripts/
    ├── extract_text.py
    ├── fill_form.py
    └── merge_pdfs.py
```

**pdf-processing/SKILL.md:**
```markdown
---
name: pdf-processing
description: Extract text and tables from PDF files, fill forms, merge documents. Use when working with PDF files, forms, document extraction, or when user mentions PDFs.
allowed-tools: Read, Bash(python:*)
---

# PDF Processing

## Overview

Process PDF files including text extraction, form filling, and document merging using pypdf and pdfplumber libraries.

## Requirements

Install required packages:
```bash
pip install pypdf pdfplumber
```

## Quick Start

### Extract Text
```python
import pdfplumber
with pdfplumber.open("document.pdf") as pdf:
    text = pdf.pages[0].extract_text()
    print(text)
```

### Fill Form
Use the bundled script:
```bash
python scripts/fill_form.py input.pdf output.pdf --data form_data.json
```

### Merge PDFs
```python
from pypdf import PdfMerger
merger = PdfMerger()
merger.append("file1.pdf")
merger.append("file2.pdf")
merger.write("combined.pdf")
```

## Instructions

1. **For text extraction:**
   - Use pdfplumber for accurate text and table extraction
   - Handle multi-page documents by iterating through pages
   - See [REFERENCE.md](REFERENCE.md) for detailed extraction options

2. **For form filling:**
   - Use the fill_form.py script for consistent results
   - Provide data as JSON with field names matching form
   - Script validates all required fields before filling

3. **For merging:**
   - Use pypdf's PdfMerger for combining documents
   - Preserve metadata and bookmarks
   - Check file sizes before merging large documents

## Common Tasks

### Extract Tables
```python
with pdfplumber.open("data.pdf") as pdf:
    table = pdf.pages[0].extract_table()
    # Returns list of lists
```

### Get Page Count
```python
from pypdf import PdfReader
reader = PdfReader("doc.pdf")
print(len(reader.pages))
```

## Additional Resources

- For complete API documentation, see [REFERENCE.md](REFERENCE.md)
- For form field mappings and validation, see REFERENCE.md Forms section
- Scripts automatically validate inputs and provide error messages

## Best Practices

- Always check PDF is not encrypted before processing
- Handle exceptions for corrupted or invalid PDFs
- Use pdfplumber for extraction, pypdf for manipulation
- Test scripts on sample files first
- Close file handles properly (use context managers)

## Error Handling

```python
try:
    with pdfplumber.open("file.pdf") as pdf:
        # Process PDF
        pass
except FileNotFoundError:
    print("PDF file not found")
except Exception as e:
    print(f"Error processing PDF: {e}")
```
```

**pdf-processing/REFERENCE.md:**
```markdown
# PDF Processing Reference

Complete API reference for PDF operations.

## pdfplumber API

### Opening PDFs
```python
pdfplumber.open(path, password=None, pages=None)
```

### Page Properties
- `page.width` - Page width in points
- `page.height` - Page height in points
- `page.bbox` - Bounding box (x0, y0, x1, y1)
- `page.chars` - List of character objects
- `page.lines` - List of line objects

### Text Extraction
```python
page.extract_text(
    x_tolerance=3,
    y_tolerance=3,
    layout=False,
    x_density=7.25,
    y_density=13
)
```

### Table Extraction
```python
page.extract_table(
    table_settings={
        "vertical_strategy": "lines",
        "horizontal_strategy": "lines",
        "explicit_vertical_lines": [],
        "explicit_horizontal_lines": [],
        "snap_tolerance": 3,
        "join_tolerance": 3,
        "edge_min_length": 3,
        "min_words_vertical": 3,
        "min_words_horizontal": 1
    }
)
```

## pypdf API

### PdfReader
```python
from pypdf import PdfReader

reader = PdfReader("file.pdf")
len(reader.pages)  # Page count
reader.metadata  # PDF metadata
reader.pages[0]  # Get page
```

### PdfWriter
```python
from pypdf import PdfWriter

writer = PdfWriter()
writer.add_page(page)
writer.write("output.pdf")
```

### PdfMerger
```python
from pypdf import PdfMerger

merger = PdfMerger()
merger.append("file1.pdf")
merger.append("file2.pdf", pages=(0, 3))  # First 3 pages
merger.write("merged.pdf")
```

## Form Fields

### Getting Form Fields
```python
reader = PdfReader("form.pdf")
fields = reader.get_fields()
for name, field in fields.items():
    print(f"{name}: {field.get('/V')}")
```

### Filling Forms
```python
from pypdf import PdfWriter, PdfReader

reader = PdfReader("form.pdf")
writer = PdfWriter()

writer.add_page(reader.pages[0])
writer.update_page_form_field_values(
    writer.pages[0],
    {"fieldname": "value"}
)
writer.write("filled_form.pdf")
```

## Common Form Field Types

- `/Tx` - Text field
- `/Btn` - Button (checkbox or radio)
- `/Ch` - Choice (dropdown or list)
- `/Sig` - Signature field

## Error Codes

- `FileNotFoundError` - PDF file doesn't exist
- `PdfReadError` - Corrupted or invalid PDF
- `PasswordError` - PDF is encrypted
- `ValueError` - Invalid parameters
```

**pdf-processing/scripts/fill_form.py:**
```python
#!/usr/bin/env python3
"""Fill PDF form fields from JSON data."""

import json
import sys
from pypdf import PdfReader, PdfWriter

def fill_form(input_pdf, output_pdf, data_file):
    """Fill PDF form with data from JSON."""
    try:
        # Load data
        with open(data_file, 'r') as f:
            data = json.load(f)
        
        # Read PDF
        reader = PdfReader(input_pdf)
        writer = PdfWriter()
        
        # Get form fields
        fields = reader.get_fields()
        if not fields:
            print("Error: No form fields found in PDF")
            return False
        
        # Validate required fields
        missing = [k for k in data.keys() if k not in fields]
        if missing:
            print(f"Warning: Fields not in form: {missing}")
        
        # Fill form
        writer.add_page(reader.pages[0])
        writer.update_page_form_field_values(
            writer.pages[0],
            data
        )
        
        # Write output
        with open(output_pdf, 'wb') as f:
            writer.write(f)
        
        print(f"Successfully filled form: {output_pdf}")
        return True
        
    except FileNotFoundError as e:
        print(f"Error: File not found - {e}")
        return False
    except json.JSONDecodeError:
        print(f"Error: Invalid JSON in {data_file}")
        return False
    except Exception as e:
        print(f"Error filling form: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: fill_form.py <input.pdf> <output.pdf> <data.json>")
        sys.exit(1)
    
    success = fill_form(sys.argv[1], sys.argv[2], sys.argv[3])
    sys.exit(0 if success else 1)
```

### Validation Results
✅ Name valid
✅ Description comprehensive with triggers
✅ Tool restrictions appropriate (Read, Python only)
✅ Dependencies documented
✅ Progressive disclosure used
✅ Scripts executable, not readable
✅ Reference docs separate
✅ Under 500 lines in SKILL.md

---

## Example 3: Read-Only Analysis Skill

### User Request
> "Create a skill that analyzes code quality without making any changes"

### Questions Asked
1. Name: `code-analyzer`
2. Tool restrictions: Read-only? Yes, absolutely
3. Should it analyze specific languages? No, general purpose
4. Generate reports? Yes, summary format

### Generated Skill

**File Structure:**
```
code-analyzer/
└── SKILL.md
```

**code-analyzer/SKILL.md:**
```markdown
---
name: code-analyzer
description: Analyze code quality, complexity, and best practices without making changes. Use for code reviews, quality checks, or when user asks to analyze or review code.
allowed-tools: Read, Grep, Glob
---

# Code Analyzer

## Overview

Performs read-only analysis of code quality, identifying issues, complexity, and adherence to best practices.

## Instructions

1. **Identify scope**
   - Single file or directory?
   - Specific language or multi-language?
   - Full analysis or specific aspects?

2. **Scan for issues**
   - Code complexity (long functions, deep nesting)
   - Naming conventions
   - Error handling patterns
   - Documentation quality
   - Potential bugs or anti-patterns

3. **Generate report with:**
   - **Summary**: High-level findings
   - **Issues by severity**: Critical, Warning, Info
   - **Metrics**: File count, line count, complexity
   - **Recommendations**: Prioritized improvements

4. **Report Format:**
   ```
   # Code Analysis Report
   
   ## Summary
   Analyzed: X files, Y lines
   Issues: Z critical, W warnings
   
   ## Critical Issues
   1. [File:Line] Description and impact
   
   ## Warnings
   1. [File:Line] Description and recommendation
   
   ## Metrics
   - Average function length: X lines
   - Maximum nesting depth: Y
   - Files needing attention: Z
   
   ## Recommendations
   1. Priority item with reasoning
   ```

## Analysis Categories

### Code Quality
- Function length (suggest <50 lines)
- Nesting depth (suggest <4 levels)
- Cyclomatic complexity
- Code duplication

### Best Practices
- Error handling present
- Input validation
- Resource cleanup
- Security patterns

### Documentation
- Function/class docstrings
- Complex logic explained
- TODOs and FIXMEs noted
- README completeness

### Naming
- Descriptive variable names
- Consistent conventions
- Avoid abbreviations
- Clear function names

## Severity Levels

- **Critical**: Security issues, bugs, crashes
- **Warning**: Bad practices, maintainability issues
- **Info**: Style suggestions, optimizations

## Language-Specific Checks

### Python
- PEP 8 style adherence
- Type hints usage
- Exception handling
- Import organization

### JavaScript/TypeScript
- ESLint rule violations
- Async/await usage
- Type safety (TS)
- Module structure

### General
- Magic numbers
- Long parameter lists
- God classes/functions
- Dead code

## Best Practices

- Focus on impactful issues first
- Provide context for each issue
- Suggest specific fixes
- Balance thoroughness with practicality
- Group similar issues

## Notes

This skill is read-only and cannot modify files. For automated fixes, use a different skill or tool.
```

### Validation Results
✅ Name valid
✅ Description with triggers
✅ Tool restrictions enforced (Read, Grep, Glob only)
✅ Clear instructions
✅ Comprehensive categories
✅ Report format defined
✅ Single file, under 500 lines

---

## Example 4: Skill with Hooks

### User Request
> "Create a skill that runs security checks before executing any bash commands"

### Questions Asked
1. Name: `secure-ops`
2. What security checks? Validate commands, log execution
3. Block dangerous commands? Yes
4. Hooks needed? Yes, PreToolUse

### Generated Skill

**File Structure:**
```
secure-ops/
├── SKILL.md
└── scripts/
    └── security-check.sh
```

**secure-ops/SKILL.md:**
```markdown
---
name: secure-ops
description: Execute operations with security validation and logging. Use when running system commands, bash scripts, or security-sensitive operations.
hooks:
  PreToolUse:
    - matcher: "Bash"
      hooks:
        - type: command
          command: "./scripts/security-check.sh $TOOL_INPUT"
          once: false
---

# Secure Operations

## Overview

Executes system operations with automatic security validation before each command execution.

## Instructions

1. **Automatic Security Checks**
   - All bash commands validated before execution
   - Dangerous patterns blocked
   - Execution logged for audit

2. **Blocked Patterns**
   - `rm -rf /` and similar destructive commands
   - Privilege escalation attempts
   - Network commands to untrusted hosts
   - File operations outside workspace

3. **When command is blocked:**
   - Review the security message
   - Modify command to be safer
   - Request explicit approval if needed

## Security Validation

The security check script validates:
- Command safety
- File path boundaries
- Network destinations
- Privilege requirements

## Best Practices

- Review command output for warnings
- Use absolute paths when possible
- Validate inputs before operations
- Check security logs if issues arise

## Audit Log

Commands are logged to `.claude/logs/security-audit.log`
```

**secure-ops/scripts/security-check.sh:**
```bash
#!/bin/bash
# Security validation for bash commands

COMMAND="$1"

# Blocked patterns
DANGEROUS_PATTERNS=(
    "rm -rf /"
    "dd if=/dev/zero"
    ":(){ :|:& };:"
    "mkfs"
    "mv / "
)

# Check for dangerous patterns
for pattern in "${DANGEROUS_PATTERNS[@]}"; do
    if [[ "$COMMAND" == *"$pattern"* ]]; then
        echo "BLOCKED: Dangerous command pattern detected"
        exit 1
    fi
done

# Log command
echo "[$(date)] $COMMAND" >> .claude/logs/security-audit.log

exit 0
```

### Validation Results
✅ Name valid
✅ Hooks configured correctly
✅ PreToolUse with Bash matcher
✅ Security script executable
✅ Instructions clear
✅ Audit logging included

---

## Example 5: Forked Context Skill

### User Request
> "Create a skill for complex refactoring that needs its own isolated workspace"

### Questions Asked
1. Name: `advanced-refactoring`
2. What types of refactoring? Class extraction, pattern implementation
3. Need isolation? Yes, multi-step process
4. Agent type? general-purpose

### Generated Skill

**File Structure:**
```
advanced-refactoring/
└── SKILL.md
```

**advanced-refactoring/SKILL.md:**
```markdown
---
name: advanced-refactoring
description: Perform complex multi-step refactoring operations with isolated context. Use for extracting classes, implementing design patterns, or large-scale code restructuring.
context: fork
agent: general-purpose
---

# Advanced Refactoring

## Overview

Performs complex refactoring operations in an isolated context, preventing main conversation clutter.

## Instructions

1. **Analyze current structure**
   - Read relevant files
   - Identify dependencies
   - Map relationships
   - Document current state

2. **Plan refactoring**
   - Define target structure
   - List transformation steps
   - Identify breaking changes
   - Plan migration path

3. **Execute transformation**
   - Make changes incrementally
   - Test after each step
   - Preserve functionality
   - Update references

4. **Validate results**
   - Run tests
   - Check for regressions
   - Verify all references updated
   - Document changes made

## Supported Refactorings

### Extract Class
- Move related methods to new class
- Update all references
- Maintain encapsulation

### Design Pattern Implementation
- Singleton, Factory, Observer, etc.
- Adapt to existing code structure
- Minimize breaking changes

### Method Extraction
- Identify code blocks for extraction
- Create well-named methods
- Reduce duplication

### Interface Extraction
- Identify common behaviors
- Create interface
- Implement across classes

## Best Practices

- Work incrementally
- Test frequently
- Maintain git commits for rollback
- Document non-obvious changes
- Update related documentation

## Isolated Context Benefits

This skill runs in a forked context, meaning:
- Separate conversation history
- Own tool access
- Doesn't clutter main chat
- Returns summary when complete
```

### Validation Results
✅ Name valid
✅ context: fork configured
✅ agent specified
✅ Appropriate for isolation
✅ Clear instructions
✅ Benefits explained

---

## Common Patterns Summary

1. **Simple skills**: Single SKILL.md, clear instructions
2. **Complex skills**: Progressive disclosure with supporting files
3. **Read-only skills**: Tool restrictions for safety
4. **Script-based skills**: Executable utilities, not readable
5. **Security-sensitive**: Hooks for validation
6. **Multi-step operations**: Forked context for isolation

Each pattern serves specific use cases while following official standards.
