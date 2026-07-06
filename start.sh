#!/bin/bash

# DE Accountant Agent One-Click Start Script
# Usage: 
#   ./start.sh         - Start via local Python venv (development mode)
#   ./start.sh docker  - Start via Docker Compose (production mode)

set -e

# Change directory to script location
cd "$(dirname "$0")"

MODE=$1

if [ "$MODE" = "docker" ]; then
    echo "==========================================="
    echo "🚀 Starting Accountant Agent via Docker Compose..."
    echo "==========================================="
    
    if [ ! -f .env ]; then
        echo "📝 First-time setup: .env file not found. Copying .env.example..."
        cp .env.example .env
        
        echo "=========================================================="
        echo "🔑 Let's configure your DeepSeek API Key."
        echo "   Please paste your Key below (or press Enter to configure manually later):"
        echo "=========================================================="
        read -r USER_KEY
        
        if [ -n "$USER_KEY" ]; then
            # Replace placeholder in .env
            sed -i '' "s/DEEPSEEK_API_KEY=.*/DEEPSEEK_API_KEY=$USER_KEY/g" .env 2>/dev/null || sed -i "s/DEEPSEEK_API_KEY=.*/DEEPSEEK_API_KEY=$USER_KEY/g" .env
            echo "✅ DeepSeek API Key successfully configured in .env."
        else
            echo "⚠️ No key entered. Remember to edit .env before running the agent!"
        fi
    fi
    
    docker-compose up -d --build
    echo "✅ Docker containers started. Running logs..."
    docker-compose logs -f
else
    echo "==========================================="
    echo "🚀 Starting Accountant Agent locally..."
    echo "==========================================="
    
    # 1. Check for Python 3
    if ! command -v python3 &> /dev/null; then
        echo "❌ Error: python3 is not installed or not in PATH."
        exit 1
    fi
    
    # 2. Check for .env file
    if [ ! -f .env ]; then
        echo "📝 First-time setup: .env file not found. Copying .env.example..."
        cp .env.example .env
        
        echo "=========================================================="
        echo "🔑 Let's configure your DeepSeek API Key."
        echo "   Please paste your Key below (or press Enter to configure manually later):"
        echo "=========================================================="
        read -r USER_KEY
        
        if [ -n "$USER_KEY" ]; then
            # Replace placeholder in .env
            sed -i '' "s/DEEPSEEK_API_KEY=.*/DEEPSEEK_API_KEY=$USER_KEY/g" .env 2>/dev/null || sed -i "s/DEEPSEEK_API_KEY=.*/DEEPSEEK_API_KEY=$USER_KEY/g" .env
            echo "✅ DeepSeek API Key successfully configured in .env."
        else
            echo "⚠️ No key entered. Remember to edit .env before running the agent!"
        fi
    fi
    
    # 3. Create virtual environment if not exists
    if [ ! -d "venv" ]; then
        echo "📦 Creating virtual environment (venv)..."
        python3 -m venv venv
    fi
    
    # 4. Activate virtual environment
    echo "🔌 Activating virtual environment..."
    source venv/bin/activate
    
    # 5. Install/update dependencies
    echo "📥 Installing/updating dependencies from requirements.txt..."
    pip install --upgrade pip
    pip install -r requirements.txt
    
    # 6. Create directories
    mkdir -p data data/config_history
    
    # 7. Start FastAPI app
    echo "🔥 Starting FastAPI server on port 8080..."
    echo "💡 Webhook endpoint: http://localhost:8080/feishu/callback"
    uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload
fi
