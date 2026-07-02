# SEO README Generator - Validation Guidelines

## Pre-Implementation Checklist

Before implementing the SEO README generator skill, ensure the following:

### 1. Codebase Analysis Capabilities
- [ ] Skill can scan entire codebase using `Glob` for file discovery
- [ ] Skill can identify project type (Node.js, Python, Java, etc.) using `Grep`
- [ ] Skill can extract project metadata from configuration files
- [ ] Skill can identify main technologies and frameworks used

### 2. SEO Optimization Features
- [ ] Skill includes relevant keywords naturally in content
- [ ] Skill implements proper heading hierarchy (H1, H2, H3)
- [ ] Skill adds meta description recommendations
- [ ] Skill includes Open Graph tag suggestions

### 3. Content Quality Standards
- [ ] Skill produces readable content for both technical and non-technical audiences
- [ ] Skill includes practical examples based on actual code
- [ ] Skill documents installation and usage accurately
- [ ] Skill follows Markdown formatting best practices

### 4. Technical Accuracy
- [ ] All code examples in README are verified against actual codebase
- [ ] Dependencies listed match those found in project files
- [ ] API endpoints documented match those implemented
- [ ] Configuration instructions align with actual project setup

## Post-Generation Validation

After generating a README, validate:

### 1. Completeness Check
- [ ] Project title and description are present
- [ ] Installation instructions are included
- [ ] Usage examples are provided
- [ ] Main features are documented
- [ ] Contribution guidelines exist
- [ ] License information is mentioned

### 2. SEO Optimization Check
- [ ] Primary keywords appear in title and headings
- [ ] Keywords are naturally integrated throughout content
- [ ] Heading hierarchy follows H1 > H2 > H3 structure
- [ ] Content is well-structured and scannable
- [ ] Meta description is included

### 3. Technical Accuracy Check
- [ ] All code examples are executable as shown
- [ ] Dependencies match package.json/requirements.txt
- [ ] API documentation matches implementation
- [ ] Configuration variables are accurately documented

## Quality Metrics

The generated README should achieve:

- Reading grade level appropriate for target audience (typically grade 8-10)
- Keyword density of 1-2% for primary keywords
- At least 500 words for comprehensive projects
- Proper Markdown formatting throughout
- All external links are valid
- All code examples follow best practices

## Common Issues to Avoid

- [ ] Generic descriptions that don't reflect actual project
- [ ] Missing critical installation dependencies
- [ ] Outdated information that doesn't match current code
- [ ] Poor heading structure that hinders readability
- [ ] Keyword stuffing that reduces readability
- [ ] Missing troubleshooting information
- [ ] Inconsistent formatting throughout document