# SEO README Generator - Examples

## Example 1: Basic README Generation

**User Request**: "Create a README for this Node.js project"

**Codebase Discovered**:
```json
// package.json
{
  "name": "express-api-boilerplate",
  "version": "1.0.0",
  "dependencies": {
    "express": "^4.18.0",
    "mongoose": "^7.0.0",
    "dotenv": "^16.0.0"
  },
  "scripts": {
    "start": "node src/index.js",
    "dev": "nodemon src/index.js"
  }
}
```

**Generated README.md**:
```markdown
<!-- SEO Meta Tags
Description: Express.js REST API boilerplate with MongoDB integration. Fast, scalable backend template for Node.js applications with authentication and CRUD operations.
Keywords: Express, Node.js, REST API, MongoDB, Backend, API Boilerplate
-->

# Express API Boilerplate - Node.js REST API Template

![Node.js](https://img.shields.io/badge/node-%3E%3D14.0.0-brightgreen)
![License](https://img.shields.io/badge/license-MIT-blue)

## Description

A production-ready Express.js REST API boilerplate with MongoDB integration. This Node.js backend template provides authentication, CRUD operations, and a scalable project structure for building modern web applications.

## Features

- ⚡ Express.js server with RESTful routing
- 📦 MongoDB integration via Mongoose
- 🔐 Environment configuration with dotenv
- 🚀 Hot reloading with nodemon
- 📁 Organized project structure

## Installation

```bash
# Clone the repository
git clone <repository-url>

# Install dependencies
npm install

# Create environment file
cp .env.example .env
```

## Configuration

Create a `.env` file with:

```env
PORT=3000
MONGODB_URI=mongodb://localhost:27017/yourdb
NODE_ENV=development
```

## Usage

```bash
# Development mode with hot reload
npm run dev

# Production mode
npm start
```

**Example Request**:
```javascript
// GET /api/users
const response = await fetch('http://localhost:3000/api/users');
const users = await response.json();
```

## Project Structure

```
project/
├── src/
│   ├── index.js          # Entry point
│   ├── routes/           # API routes
│   ├── models/           # Mongoose models
│   └── controllers/      # Route controllers
├── package.json
└── .env.example
```

## Dependencies

- **express** (^4.18.0) - Web framework for Node.js
- **mongoose** (^7.0.0) - MongoDB object modeling
- **dotenv** (^16.0.0) - Environment variable management

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT

## Troubleshooting

**MongoDB Connection Error**: Ensure MongoDB is running and URI is correct in `.env`

**Port Already in Use**: Change PORT in `.env` to an available port
```

**SEO Analysis**:
- ✅ Keywords: Express, Node.js, REST API, MongoDB (density: 1.8%)
- ✅ Heading hierarchy: H1 → H2 → proper
- ✅ Meta tags included
- ✅ Word count: 287 words

## Example 2: Python Project Documentation

**User Request**: "Generate an SEO-optimized README for this Django application"

**Codebase Discovered**:
```toml
# pyproject.toml
[project]
name = "django-blog-api"
version = "1.0.0"
dependencies = [
    "django>=4.2",
    "djangorestframework>=3.14",
    "psycopg2-binary>=2.9"
]
```

**Generated README.md** (excerpt):
```markdown
# Django Blog API - Python REST Framework Backend

![Python](https://img.shields.io/badge/python-3.11+-blue)
![Django](https://img.shields.io/badge/django-4.2-green)

## Description

A modern blog API built with Django and Django REST Framework. This Python web application provides RESTful endpoints for managing blog posts, user authentication, and comments with PostgreSQL database support.

## Features

- 📦 Django 4.2 with REST Framework
- 🔐 JWT authentication
- 📝 CRUD operations for blog posts
- 💬 Comment system
- 📊 PostgreSQL database integration

## Installation

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -e .

# Run migrations
python manage.py migrate

# Create superuser
python manage.py createsuperuser
```

## Configuration

```python
# settings.py (key configurations)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('DB_NAME', 'blogdb'),
        'USER': os.environ.get('DB_USER', 'postgres'),
        'PASSWORD': os.environ.get('DB_PASSWORD'),
        'HOST': os.environ.get('DB_HOST', 'localhost'),
        'PORT': os.environ.get('DB_PORT', '5432'),
    }
}
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET    | `/api/posts/` | List all posts |
| POST   | `/api/posts/` | Create post |
| GET    | `/api/posts/{id}/` | Get specific post |
| PUT    | `/api/posts/{id}/` | Update post |
| DELETE | `/api/posts/{id}/` | Delete post |

## Usage

```bash
# Start development server
python manage.py runserver
```

**API Request Example**:
```python
import requests

response = requests.get('http://localhost:8000/api/posts/')
posts = response.json()
```

## Troubleshooting

**Database Connection Error**: Ensure PostgreSQL is running and credentials are correct

**Migration Issues**: Try `python manage.py migrate --run-syncdb`
```

## Example 3: CLI Tool Documentation

**User Request**: "Document this command-line tool"

**Codebase Discovered**:
```json
// package.json
{
  "name": "file-organizer-cli",
  "version": "1.0.0",
  "bin": {
    "organize": "./bin/cli.js"
  },
  "dependencies": {
    "commander": "^10.0.0",
    "chalk": "^5.2.0"
  }
}
```

**Generated README.md**:
```markdown
# File Organizer CLI - Automated File Management Tool

## Description

A powerful Node.js command-line tool for automatically organizing files by type, date, or custom rules. Simplify file management and keep directories clean with this CLI utility.

## Features

- 📁 Organize files by extension, date, or size
- ⚡ Fast batch processing
- 🎨 Colorful terminal output
- ⚙️ Configurable rules via JSON

## Installation

```bash
# Global installation
npm install -g file-organizer-cli

# Or use with npx
npx file-organizer-cli
```

## Usage

```bash
# Organize current directory by file type
organize --type

# Organize by date
organize --date ~/Downloads

# Use custom rules
organize --config rules.json

# Dry run (preview without moving)
organize --dry-run --type
```

**Options**:
- `--type, -t`: Organize by file extension
- `--date, -d`: Organize by creation date
- `--config, -c <file>`: Use custom rule file
- `--dry-run`: Preview changes without executing
- `--help, -h`: Show help

## Configuration

Create `rules.json`:
```json
{
  "images": ["jpg", "png", "gif"],
  "documents": ["pdf", "docx", "txt"],
  "videos": ["mp4", "avi", "mkv"]
}
```

## Examples

```bash
# Organize Downloads folder
organize -t ~/Downloads

# Preview organization
organize --dry-run --type
```

## Troubleshooting

**Permission Denied**: Run with appropriate permissions or use sudo (Linux/Mac)

**Command Not Found**: Reinstall globally or check PATH
```

---

## Example 4: React Frontend Project

**User Request**: "Write a technical README for this React project"

**Key Elements Generated**:
- Keywords: React, JavaScript, Frontend, SPA, Components
- Development setup: `npm install` → `npm start`
- Build instructions: `npm run build`
- Component architecture overview
- State management (Redux/Context API)
- Routing structure
- Environment variables for API endpoints

---

## Example 5: Full-Stack MERN Application

**Key Elements Generated**:
- Keywords: MERN Stack, MongoDB, Express, React, Node.js, Full-Stack
- Separate setup sections for frontend and backend
- Database schema with Mongoose models
- API documentation table
- Environment configs for client and server
- Deployment instructions (Heroku/Vercel/Railway)

---

## Example 6: Python Library/Package

**Key Elements Generated**:
- Keywords: Python Library, Package, PyPI, Utility Functions
- Installation: `pip install package-name`
- Import examples: `from package import function`
- API documentation with parameters and return types
- Type hints and docstring examples
- Contributing guidelines for open source
- Testing instructions with pytest