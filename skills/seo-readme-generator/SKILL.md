---
name: seo-readme-generator
description: Generates modern, SEO-optimized README files with badges, shields, tags, social media cards, and GitHub metadata. Analyzes codebase to create beautiful, searchable documentation with Open Graph tags, Twitter cards, and comprehensive structure. Use when user asks to create README, generate docs, or wants modern/SEO README.
allowed-tools: Read, Grep, Glob, Write, Create
version: 1.0.0
---

# SEO README Generator

## Overview

This skill generates SEO-optimized technical README files by scanning and analyzing the entire codebase. It creates structured, searchable documentation with proper heading hierarchy, relevant keywords, meta descriptions, and Open Graph tags to improve discoverability and search rankings.

## Instructions

### Step 1: Check for Existing README

1. Check if README.md already exists
   - If it exists and is comprehensive, ask user: "A README already exists. Should I: (a) enhance it, (b) replace it, or (c) create a new version?"
   - If it's minimal or missing, proceed with generation

### Step 2: Analyze Codebase Efficiently

**Priority 0: Repository Context Detection** (MUST DO FIRST):
- Read `.git/config` to extract:
  - Repository owner/username from `url = https://github.com/[owner]/[repo].git`
  - Repository name from same URL
- If `.git/config` not found, use workspace folder name as repo name
- Read `LICENSE` or `LICENSE.txt` to detect license type:
  - Search for "Apache License" → "Apache 2.0"
  - Search for "MIT License" → "MIT"
  - Search for "GNU General Public License" → "GPL-3.0"
  - If not found, use "MIT" as default
- Run `git config user.name` to get author name (if git available)
- Store these values: `owner`, `repo`, `license`, `author`

**Priority 1: Configuration files** (most informative):
- Check `package.json`, `requirements.txt`, `pyproject.toml`, `Cargo.toml`
- Extract: name, version, dependencies, scripts

**Priority 2: Project structure**:
- Use `Glob` to find patterns: `**/*.{js,py,java,go,rs}`
- Count files per language (don't read all)
- Identify main directories: `src/`, `lib/`, `tests/`

**Priority 3: Entry points**:
- Find `index.js`, `main.py`, `app.js`, `server.js`
- Use `Grep` for imports, routes, class definitions

**Edge cases**:
- No config files → Ask: "What type of project is this?"
- Large codebase (>1000 files) → Focus on main directories only
- Unclear tech stack → List files and ask user

**See REFERENCE.md** for complete analysis patterns and tech stack detection.

### Step 3: Generate README Using Modern Template Structure

Create README.md with this comprehensive structure:

```markdown
<!-- SEO Meta Tags -->
<!-- 
Description: [150-160 char SEO description with primary keywords]
Keywords: [primary-keyword], [secondary-keyword], [tech-stack], [use-case]
author: [Author Name]
canonical: [Repository URL]
-->

<!-- Open Graph / Facebook -->
<!--
og:type: website
og:url: [Repository URL]
og:title: [Project Name] - [Brief Tagline]
og:description: [Compelling 2-3 sentence description]
og:image: [Social preview image URL - 1200x630px]
-->

<!-- Twitter Card -->
<!--
twitter:card: summary_large_image
twitter:url: [Repository URL]
twitter:title: [Project Name] - [Brief Tagline]
twitter:description: [Compelling description]
twitter:image: [Social preview image URL]
twitter:creator: [@TwitterHandle]
-->

<!-- GitHub Metadata -->
<!--
topics: [topic1], [topic2], [topic3]
languages: [primary-language], [secondary-language]
-->

<div align="center">

# 🚀 [Project Name]

### [Compelling Tagline with Keywords]

[![License](https://img.shields.io/badge/license-[DETECTED_LICENSE]-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-[DETECTED_VERSION]-brightgreen.svg)](#)
[![Build Status](https://img.shields.io/badge/build-passing-success.svg)](#)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#)

<!-- Use detected values from Step 2:
   [DETECTED_LICENSE]: License from LICENSE file (Apache 2.0, MIT, GPL-3.0)
   [DETECTED_VERSION]: Version from package.json/pyproject.toml
   [owner]: GitHub username from .git/config
   [repo]: Repository name from .git/config or folder name
-->

<!-- Tech Stack Badges -->
![Primary Language](https://img.shields.io/badge/[Language]-[Version]-[Color].svg)
![Framework](https://img.shields.io/badge/[Framework]-[Version]-[Color].svg)
![Database](https://img.shields.io/badge/[Database]-[Version]-[Color].svg)

<!-- Stats Badges -->
![GitHub Stars](https://img.shields.io/github/stars/[owner]/[repo]?style=social)
![GitHub Forks](https://img.shields.io/github/forks/[owner]/[repo]?style=social)
![GitHub Issues](https://img.shields.io/github/issues/[owner]/[repo])
![GitHub Pull Requests](https://img.shields.io/github/issues-pr/[owner]/[repo])

[**Explore Docs**](#) • [**Report Bug**](#) • [**Request Feature**](#)

</div>

---

## 📋 Table of Contents

- [About](#about)
- [Features](#features)
- [Demo](#demo)
- [Tech Stack](#tech-stack)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
- [Usage](#usage)
- [API Reference](#api-reference)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)
- [Contact](#contact)
- [Acknowledgments](#acknowledgments)

---

## 🎯 About

<div align="center">
  <img src="[project-logo-or-screenshot.png]" alt="Project Screenshot" width="600"/>
</div>

**[Project Name]** is a [brief description with primary keywords]. Built with [tech stack], this [project type] helps [target audience] to [main benefit].

### Why This Project?

- ✨ **[Key Benefit 1]**: [Explanation]
- 🚀 **[Key Benefit 2]**: [Explanation]
- 💡 **[Key Benefit 3]**: [Explanation]

---

## ✨ Features

<table>
  <tr>
    <td>
      
**🎨 [Feature Category 1]**
- Feature 1 with keywords
- Feature 2 with keywords
- Feature 3 with keywords

    </td>
    <td>
      
**⚡ [Feature Category 2]**
- Feature 4 with keywords
- Feature 5 with keywords
- Feature 6 with keywords

    </td>
  </tr>
  <tr>
    <td>
      
**🔒 [Feature Category 3]**
- Feature 7 with keywords
- Feature 8 with keywords

    </td>
    <td>
      
**📊 [Feature Category 4]**
- Feature 9 with keywords
- Feature 10 with keywords

    </td>
  </tr>
</table>

---

## 🎬 Demo

### Live Demo

🔗 **[Live Application](https://demo-url.com)**

### Screenshots

<details>
<summary>Click to view screenshots</summary>

![Screenshot 1](path/to/screenshot1.png)
*Caption describing screenshot 1*

![Screenshot 2](path/to/screenshot2.png)
*Caption describing screenshot 2*

</details>

### Video Demo

[![Demo Video](thumbnail.png)](https://youtube.com/watch?v=demo)

---

## 🛠️ Tech Stack

<div align="center">

**Frontend** • **Backend** • **Database** • **DevOps**

</div>

| Category | Technologies |
|----------|-------------|
| **Frontend** | ![Tech1](badge-url) ![Tech2](badge-url) |
| **Backend** | ![Tech3](badge-url) ![Tech4](badge-url) |
| **Database** | ![Tech5](badge-url) |
| **DevOps** | ![Tech6](badge-url) ![Tech7](badge-url) |
| **Tools** | ![Tool1](badge-url) ![Tool2](badge-url) |

<details>
<summary><b>📦 View all dependencies</b></summary>

### Core Dependencies

```json
[List from package.json/requirements.txt with versions and purposes]
```

### Dev Dependencies

```json
[Development dependencies]
```

</details>

---

## 🚀 Getting Started

### Prerequisites

Before you begin, ensure you have:

- ✅ [Requirement 1] ([version])
- ✅ [Requirement 2] ([version])
- ✅ [Requirement 3] ([version])

### Installation

1️⃣ **Clone the repository**

```bash
git clone https://github.com/[owner]/[repo].git
cd [repo]
```

2️⃣ **Install dependencies**

```bash
[Actual install command from package.json/requirements.txt]
```

3️⃣ **Set up environment**

```bash
cp .env.example .env
# Edit .env with your configuration
```

4️⃣ **Run the application**

```bash
[Actual run command from scripts]
```

🎉 **Success!** Navigate to `http://localhost:[PORT]`

---

## 💻 Usage

### Basic Example

```[language]
[Real, executable code example from entry point]
```

### Advanced Examples

<details>
<summary><b>Example 1: [Use Case]</b></summary>

```[language]
[Advanced example code]
```

**Output:**
```
[Expected output]
```

</details>

<details>
<summary><b>Example 2: [Another Use Case]</b></summary>

```[language]
[Another advanced example]
```

</details>

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `option1` | `string` | `value` | Description with keywords |
| `option2` | `number` | `value` | Description with keywords |
| `option3` | `boolean` | `false` | Description with keywords |

---

## 📚 API Reference

<details>
<summary><b>View API Endpoints</b></summary>

### Authentication

```http
POST /api/auth/login
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `email` | `string` | **Required**. User email |
| `password` | `string` | **Required**. User password |

**Response:**
```json
{
  "token": "jwt-token",
  "user": { "id": 1, "email": "user@example.com" }
}
```

### [Other Endpoints...]

</details>

---

## 🗂️ Project Structure

```
[Project Name]/
├── 📁 src/               # Source code
│   ├── 📁 components/   # UI components
│   ├── 📁 services/     # Business logic
│   └── 📄 index.js      # Entry point
├── 📁 tests/            # Test files
├── 📁 docs/             # Documentation
├── 📄 package.json      # Dependencies
└── 📄 README.md         # You are here
```

---

## 🗺️ Roadmap

- [x] ✅ Core functionality
- [x] ✅ Basic features
- [ ] 🚧 Feature in progress
- [ ] 📅 Planned feature 1
- [ ] 📅 Planned feature 2

See the [open issues](https://github.com/[owner]/[repo]/issues) for a full list of proposed features (and known issues).

---

## 🤝 Contributing

Contributions make the open source community amazing! Any contributions you make are **greatly appreciated**.

<details>
<summary><b>How to Contribute</b></summary>

1. 🍴 Fork the Project
2. 🌿 Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. ✍️ Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. 🚀 Push to the Branch (`git push origin feature/AmazingFeature`)
5. 🎉 Open a Pull Request

### Contribution Guidelines

- Follow code style conventions
- Write meaningful commit messages
- Add tests for new features
- Update documentation

</details>

---

## 📄 License

Distributed under the [LICENSE NAME] License. See `LICENSE` file for more information.

---

## 📧 Contact

**[Your Name]** - [@twitter_handle](https://twitter.com/handle) - email@example.com

**Project Link**: [https://github.com/[owner]/[repo]](https://github.com/[owner]/[repo])

---

## 🙏 Acknowledgments

- [Resource or inspiration 1]
- [Resource or inspiration 2]
- [Library or tool used]

---

<div align="center">

**[Project Name]** • Built with 🖤 by [Your Name]

⭐ Star this repo if you find it helpful!

[![GitHub Stars](https://img.shields.io/github/stars/[owner]/[repo]?style=social)](https://github.com/[owner]/[repo]/stargazers)
[![Twitter Follow](https://img.shields.io/twitter/follow/[handle]?style=social)](https://twitter.com/[handle])

</div>
```

### Step 4: Generate Badges and Shields

1. **Essential Badges** (always include):
   ```markdown
   ![License](https://img.shields.io/badge/license-[DETECTED_LICENSE]-blue.svg)
   ![Version](https://img.shields.io/badge/version-[VERSION]-brightgreen.svg)
   ![Language](https://img.shields.io/badge/[LANGUAGE]-[VERSION]-[COLOR].svg)
   ```
   - Use `[DETECTED_LICENSE]` from Step 2 (e.g., "Apache%202.0", "MIT", "GPL--3.0")
   - URL-encode spaces: "Apache 2.0" → "Apache%202.0"

2. **Technology Stack Badges**:
   - Node.js: `![Node.js](https://img.shields.io/badge/node.js-[version]-339933?logo=nodedotjs)`
   - Python: `![Python](https://img.shields.io/badge/python-[version]-3776AB?logo=python)`
   - React: `![React](https://img.shields.io/badge/react-[version]-61DAFB?logo=react)`
   - Django: `![Django](https://img.shields.io/badge/django-[version]-092E20?logo=django)`
   - Express: `![Express](https://img.shields.io/badge/express-[version]-000000?logo=express)`
   - MongoDB: `![MongoDB](https://img.shields.io/badge/mongodb-[version]-47A248?logo=mongodb)`
   - PostgreSQL: `![PostgreSQL](https://img.shields.io/badge/postgresql-[version]-4169E1?logo=postgresql)`

3. **GitHub Stats Badges** (if repository exists):
   ```markdown
   ![Stars](https://img.shields.io/github/stars/[owner]/[repo]?style=social)
   ![Forks](https://img.shields.io/github/forks/[owner]/[repo]?style=social)
   ![Issues](https://img.shields.io/github/issues/[owner]/[repo])
   ![PRs](https://img.shields.io/github/issues-pr/[owner]/[repo])
   ![Contributors](https://img.shields.io/github/contributors/[owner]/[repo])
   ![Last Commit](https://img.shields.io/github/last-commit/[owner]/[repo])
   ```

4. **Quality & CI/CD Badges** (if applicable):
   ```markdown
   ![Build](https://img.shields.io/badge/build-passing-success.svg)
   ![Coverage](https://img.shields.io/badge/coverage-90%25-brightgreen.svg)
   ![Code Quality](https://img.shields.io/badge/code%20quality-A-success.svg)
   ```

5. **Badge Colors by Status**:
   - Success/Passing: `brightgreen` or `success` (#4c1)
   - Version/Info: `blue` (#007ec6)
   - License: `blue` (#007ec6)
   - Warning: `yellow` (#dfb317)
   - Error/Failing: `red` (#e05d44)

### Step 5: Apply SEO Optimization

1. **Keywords**:
   - Primary: tech stack (e.g., "React", "Python", "API")
   - Secondary: use case (e.g., "dashboard", "automation", "CLI")
   - Include in: title, headings, first paragraph, meta tags
   - Keep density at 1-2% (natural integration)

2. **Heading hierarchy**:
   - H1: Project title only (once) with emoji
   - H2: Main sections with emojis (📋 📚 🚀 💻 etc.)
   - H3: Subsections within H2s
   - Include keywords in H2/H3 headings where natural

3. **SEO Meta tags** (comprehensive HTML comments at top):
   ```html
   <!-- SEO Meta Tags
   Description: [150-160 char summary with all primary keywords]
   Keywords: [primary-tech], [secondary-tech], [use-case], [features]
   author: [Author from package.json or git config]
   canonical: [Repository URL]
   -->
   ```

4. **Open Graph tags** (for Facebook, LinkedIn sharing):
   ```html
   <!-- Open Graph / Facebook
   og:type: website
   og:url: [Full repository URL]
   og:title: [Project Name] - [Compelling tagline with keywords]
   og:description: [Compelling 2-3 sentence description with keywords]
   og:image: [Social preview image URL - optimal: 1200x630px]
   og:image:alt: [Description of the image]
   og:site_name: [Project Name]
   og:locale: en_US
   -->
   ```

5. **Twitter Card tags** (for Twitter sharing):
   ```html
   <!-- Twitter Card
   twitter:card: summary_large_image
   twitter:url: [Repository URL]
   twitter:title: [Project Name] - [Tagline]
   twitter:description: [Compelling description - same as OG]
   twitter:image: [Same social preview image]
   twitter:creator: [@TwitterHandle from package.json or git]
   twitter:site: [@OrganizationTwitter if applicable]
   -->
   ```

6. **GitHub-specific metadata** (as HTML comments):
   ```html
   <!-- GitHub Metadata
   topics: [topic1], [topic2], [topic3], [topic4], [topic5]
   languages: [primary-language], [secondary-language]
   homepage: [Project website or demo URL]
   -->
   ```

7. **Visual Enhancements**:
   - Use emojis strategically in headings (📋 🚀 💻 ✨ 🛠️ etc.)
   - Center-align title section with `<div align="center">`
   - Use collapsible sections with `<details>` for long content
   - Add horizontal rules `---` between major sections
   - Include Table of Contents with anchor links
   - Use tables for structured data (features, API, config)

### Step 6: Validate Generated README

1. **Verify accuracy**:
   - All code examples are executable
   - Dependencies match actual config files
   - Installation commands work for the detected tech stack
   - API endpoints match code (if included)

2. **Check SEO quality**:
   - Primary keywords appear in title, meta tags, and first paragraph
   - Heading hierarchy is correct (H1 → H2 → H3)
   - No keyword stuffing (sounds natural)
   - Meta description is 150-160 chars
   - Open Graph tags complete (title, description, image, url, type)
   - Twitter Card tags complete (card type, title, description, image)
   - GitHub topics relevant (3-5 topics)

3. **Validate Badges**:
   - All badge URLs are properly formatted
   - Technology versions match package.json/requirements.txt
   - Colors are appropriate (green for success, blue for info, etc.)
   - GitHub stats badges have correct owner/repo from Step 2
   - Essential badges present (license from Step 2, version, language)
   - License badge matches detected license type (not hardcoded "MIT")

3.5. **Validate Emoji Consistency**:
   - Footer MUST use black heart 🖤 (not red ❤️)
   - Section emojis are appropriate and consistent
   - No emoji overuse (max 1 per heading)

4. **Validate Markdown**:
   - All code blocks have language tags
   - Lists are properly formatted
   - Links are valid (if any)
   - Tables are well-structured (if any)

4. **Ensure completeness**:
   - All essential sections present
   - No placeholder text like "[TODO]" or "[Add details]"
   - At least 500 words for substantial projects
   - Troubleshooting section addresses common issues

## Output Format

**Always create/update README.md file** unless user specifies otherwise.

After generation, show summary:
```
✅ README.md generated successfully!

📊 Content Stats:
- Words: [count]
- Sections: [count]
- Code examples: [count]
- Primary keywords: [list]

🎨 Modern Elements:
- Badges: [count] (License, Version, Tech Stack, GitHub Stats)
- Emojis: [✓/✗] (Used in headings)
- Table of Contents: [✓/✗]
- Collapsible sections: [count]
- Center-aligned title: [✓/✗]

💡 SEO Score: [Excellent/Good/Needs Improvement]
- Keyword density: [%]
- Heading structure: [✓/✗]
- SEO meta tags: [✓/✗]
- Open Graph tags: [✓/✗]
- Twitter Card tags: [✓/✗]
- GitHub metadata: [✓/✗]
- Social preview ready: [✓/✗]

🔗 Links Generated:
- Documentation: [count]
- Internal anchors: [count]
- External resources: [count]

📝 Next steps:
1. Add actual screenshots/GIFs to make it visual
2. Create social preview image (1200x630px recommended)
3. Update GitHub repository topics
4. Test all links and badges
5. Review on GitHub to see live rendering
```

## Best Practices

- **Efficiency first**: Start with config files, don't read entire codebase
- **Accuracy over completeness**: Better to skip sections than include wrong info
- **Natural keywords**: Integrate keywords conversationally, avoid stuffing
- **Real examples**: Use actual code from the project, not generic placeholders
- **Accessibility**: Write for grade 8-10 reading level
- **Validation**: Always verify code examples are executable
- **User confirmation**: Ask before overwriting comprehensive existing READMEs

## Error Handling

**If unable to determine project type**:
- List discovered files and ask user
- Don't guess - ask clarifying questions

**If codebase is too large**:
- Focus on main directories (src/, lib/, app/)
- Sample key files rather than reading all

**If critical info missing**:
- Generate what's possible
- Add clear [TODO] markers for missing sections
- Inform user what couldn't be determined

## Additional Resources

For detailed SEO techniques and schema markup, see [REFERENCE.md](REFERENCE.md).
For project-specific examples, see [EXAMPLES.md](EXAMPLES.md).