# 🎤 Real-Time Bilingual Church Translation System

A complete, production-ready system for real-time bilingual (English ⇄ Spanish) translation designed to run on a local network from a low-power PC or Raspberry Pi. Perfect for churches, community centers, and multilingual gatherings.

## ✨ Features

- **🔴 Real-Time Translation**: Instant speech-to-text and translation using Soniox API
- **🎥 Projector Display**: Dual-language side-by-side display for projection screens
- **📱 Personal Device View**: Mobile-friendly single-language display for attendees
- **🎤 Browser Audio Streaming**: Start new sessions from any browser's microphone
- **🔀 Multi-Session Support**: Run multiple simultaneous translation sessions (main service, youth group, Bible study, etc.)
- **🧪 Testing Mode**: Test the entire system without API calls or audio hardware
- **🌐 Local Network**: Works entirely on your local network—no internet required (except for Soniox API)
- **💪 Robust & Resilient**: Automatic reconnection, error handling, and graceful degradation

---

## 📋 Table of Contents

1. [Requirements](#requirements)
2. [Installation](#installation)
3. [Configuration](#configuration)
4. [Running the System](#running-the-system)
5. [Usage Guide](#usage-guide)
6. [Testing Mode](#testing-mode)
7. [Troubleshooting](#troubleshooting)
8. [Architecture](#architecture)
9. [API Reference](#api-reference)

---

## 🔧 Requirements

### Hardware
- **Server**: Raspberry Pi 4 (2GB+ RAM) or any PC
- **Audio Input** (for main session): USB microphone or line-in connection
- **Network**: Local network (WiFi or Ethernet)
- **Displays**: Any device with a web browser

### Software
- **Python**: 3.7 or higher
- **Operating System**: Linux, macOS, or Windows
- **Browser**: Modern browser (Chrome, Firefox, Safari, Edge)

---

## 📦 Installation

### Step 1: Clone or Download the Project

```bash
# If you have git
git clone https://github.com/your-org/church-translation-system.git
cd church-translation-system

# Or download and extract the ZIP file
```

### Step 2: Install Python Dependencies

```bash
# Create a virtual environment (recommended)
python3 -m venv venv

# Activate virtual environment
# On Linux/Mac:
source venv/bin/activate
# On Windows:
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Step 3: Install Audio Libraries (For Local Audio Capture)

**On Raspberry Pi / Debian / Ubuntu:**
```bash
sudo apt-get update
sudo apt-get install portaudio19-dev python3-pyaudio
```

**On macOS:**
```bash
brew install portaudio
```

**On Windows:**
PyAudio should install automatically via pip. If you encounter issues:
```bash
pip install pipwin
pipwin install pyaudio
```

### Step 4: Install Soniox SDK (Optional - For Production)

```bash
pip install soniox
```

---

## ⚙️ Configuration

### Step 1: Create Environment File

```bash
cp .env.example .env
```

### Step 2: Edit `.env` File

Open `.env` in a text editor and configure:

```bash
# Required for production translation
SONIOX_API_KEY=your_api_key_here

# Set to 'true' for testing without API
TESTING_MODE=false

# Server settings
DEBUG=false
HOST=0.0.0.0
PORT=5000
```

### Getting a Soniox API Key

1. Visit [https://soniox.com](https://soniox.com)
2. Sign up for an account
3. Navigate to API Keys section
4. Copy your API key to `.env`

### Testing Without Soniox

Set `TESTING_MODE=true` in `.env` to run with dummy translations (no API key needed).

---

## 🚀 Running the System

### Basic Start

```bash
# Activate virtual environment
source venv/bin/activate  # or venv\Scripts\activate on Windows

# Start the server
python server.py
```

### Start with Environment Variables

```bash
# Testing mode
TESTING_MODE=true python server.py

# Production mode with API key
SONIOX_API_KEY=your_key_here python server.py
```

### Auto-Start on Boot (Raspberry Pi)

Create a systemd service:

```bash
sudo nano /etc/systemd/system/church-translation.service
```

Add:

```ini
[Unit]
Description=Church Translation System
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/church-translation-system
Environment="PATH=/home/pi/church-translation-system/venv/bin"
EnvironmentFile=/home/pi/church-translation-system/.env
ExecStart=/home/pi/church-translation-system/venv/bin/python server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl enable church-translation.service
sudo systemctl start church-translation.service
```

---

## 📖 Usage Guide

### Accessing the System

1. **Find Your Server IP**:
   ```bash
   # On Linux/Mac
   hostname -I
   
   # On Windows
   ipconfig
   ```

2. **Open Browser**: Navigate to `http://<SERVER_IP>:5000`

### Client Types

#### 🎥 **Projector Display** (`/projector`)

**Use Case**: Main sanctuary projection screen

**Features**:
- Dual-column display (English | Spanish)
- Large, readable text
- Auto-scrolling translation history
- Active speaker highlighting

**Setup**:
1. Open `/projector` on computer connected to projector
2. Select session (default: "main")
3. Press F11 for fullscreen
4. Translations appear automatically

#### 📱 **Personal Device** (`/personal`)

**Use Case**: Individual attendee phones/tablets

**Features**:
- Single-language display
- Language preference selection
- Mobile-optimized UI
- Low bandwidth usage

**Setup**:
1. Attendees open `/personal` on their phones
2. Select session (e.g., "main", "youth")
3. Choose preferred language (English or Español)
4. View translations in real-time

#### 🎤 **Audio Streamer** (`/streamer`)

**Use Case**: Starting new sessions from a browser

**Features**:
- Browser-based audio capture
- Create new translation sessions
- No server audio hardware needed
- Perfect for breakout rooms

**Setup**:
1. Meeting leader opens `/streamer`
2. Enter session ID (e.g., "youth", "bible_study")
3. Select microphone source
4. Click "Create Session" → "Start Streaming"
5. Attendees can now join this session from projector/personal views

### Session Management

The system supports multiple simultaneous sessions:

- **main**: Auto-started on server launch (uses server's microphone)
- **youth**: Youth group meeting
- **bible_study**: Bible study room
- **custom**: Any custom session ID

Each session is isolated—clients only receive translations from their joined session.

---

## 🧪 Testing Mode

Perfect for testing the system without live audio or API costs.

### Enable Testing Mode

```bash
# Option 1: Environment variable
TESTING_MODE=true python server.py

# Option 2: Edit .env file
TESTING_MODE=true
```

### What Testing Mode Does

- ✅ **No Soniox API calls** (no API key needed)
- ✅ **No audio hardware required**
- ✅ **Generates realistic dummy translations**
- ✅ **Simulates progressive word-by-word updates**
- ✅ **Tests all socket connections and UI updates**

### Testing Mode Output

The system generates phrases like:
- "Welcome to our church service today" → "Bienvenidos a nuestro servicio de iglesia hoy"
- "Let us pray together" → "Oremos juntos"
- Alternates between English and Spanish

---

## 🔍 Troubleshooting

### Server Won't Start

**Problem**: `Address already in use`
```bash
# Find process using port 5000
sudo lsof -i :5000
# Kill the process
kill -9 <PID>
```

**Problem**: `ImportError: No module named 'flask'`
```bash
# Make sure virtual environment is activated
source venv/bin/activate
pip install -r requirements.txt
```

### Audio Issues

**Problem**: "Failed to start audio"
```bash
# Check audio devices
python -c "import pyaudio; p = pyaudio.PyAudio(); print(p.get_device_count())"

# Test microphone
arecord -l  # Linux
# Or use System Preferences on Mac
```

**Problem**: No audio being captured
- Check microphone is default input device
- Test with: `arecord -d 5 test.wav` (Linux)
- Verify microphone permissions in browser (for Audio Streamer)

### Network Issues

**Problem**: Clients can't connect

1. **Check firewall**:
   ```bash
   # Linux
   sudo ufw allow 5000/tcp
   
   # Windows
   # Allow Python through Windows Firewall
   ```

2. **Verify server is listening**:
   ```bash
   netstat -an | grep 5000
   ```

3. **Test connection**:
   ```bash
   curl http://<SERVER_IP>:5000/health
   ```

### Translation Issues

**Problem**: No translations appearing

1. **Check Soniox API key**:
   ```bash
   echo $SONIOX_API_KEY
   ```

2. **Check server logs**:
   ```bash
   tail -f translation_server.log
   ```

3. **Verify session is active**:
   ```bash
   curl http://<SERVER_IP>:5000/api/sessions
   ```

### Browser Issues

**Problem**: Audio Streamer can't access microphone
- Use HTTPS or localhost (browser security requirement)
- Grant microphone permissions when prompted
- Try different browser (Chrome recommended)

**Problem**: Translations delayed or stuttering
- Check network speed: `ping <SERVER_IP>`
- Reduce number of historical items displayed
- Close unnecessary browser tabs

---

## 🏗️ Architecture

### System Components

```
┌─────────────────────────────────────────────────────────────┐
│                         SERVER                               │
│  ┌──────────────────────────────────────────────────────┐  │
│  │             Flask + Flask-SocketIO                    │  │
│  └──────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────┐  │
│  │           SessionManager                              │  │
│  │  - Manages multiple sessions                          │  │
│  │  - Routes audio/translations                          │  │
│  └──────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────┐  │
│  │   SonioxClient (per session)                          │  │
│  │  - Connects to Soniox API                             │  │
│  │  - Processes audio → translation                      │  │
│  │  - Broadcasts to clients                              │  │
│  └──────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────┐  │
│  │   AudioCapture (for main session)                     │  │
│  │  - PyAudio microphone capture                         │  │
│  │  - Feeds to SonioxClient                              │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                           │
                    WebSocket (Socket.IO)
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                   │
   ┌────▼─────┐     ┌─────▼─────┐      ┌────▼─────┐
   │Projector │     │ Personal  │      │  Audio   │
   │ Display  │     │  Device   │      │ Streamer │
   └──────────┘     └───────────┘      └──────────┘
```

### Data Flow

1. **Audio Input** → Server (PyAudio or Browser)
2. **Server** → Soniox API (streaming)
3. **Soniox API** → Server (translation results)
4. **Server** → Clients via WebSocket (room-based broadcast)
5. **Clients** → Display translations

### Session Isolation

Each session operates independently:
- Separate SonioxClient thread
- Separate audio queue
- Separate WebSocket room
- Clients only receive their session's translations

---

## 📡 API Reference

### REST Endpoints

#### `GET /health`
Health check

**Response**:
```json
{
  "status": "healthy",
  "active_sessions": 2,
  "testing_mode": false,
  "soniox_enabled": true
}
```

#### `GET /api/sessions`
List all active sessions

**Response**:
```json
{
  "success": true,
  "sessions": {
    "main": {
      "session_id": "main",
      "use_local_audio": true,
      "testing_mode": false,
      "created_at": "2024-01-15T10:30:00",
      "client_count": 5
    }
  }
}
```

#### `POST /api/sessions/<session_id>`
Create a new session

**Body**:
```json
{
  "use_local_audio": false,
  "testing_mode": false
}
```

**Response**:
```json
{
  "success": true,
  "session_id": "youth",
  "message": "Session 'youth' created"
}
```

#### `DELETE /api/sessions/<session_id>`
Stop a session

**Response**:
```json
{
  "success": true,
  "session_id": "youth",
  "message": "Session 'youth' stopped"
}
```

### WebSocket Events

#### Client → Server

**`join_session`**
```javascript
socket.emit('join_session', {
  session_id: 'main'
});
```

**`leave_session`**
```javascript
socket.emit('leave_session', {
  session_id: 'main'
});
```

**`start_session`**
```javascript
socket.emit('start_session', {
  session_id: 'youth',
  use_local_audio: false,
  testing_mode: false
});
```

**`stop_session`**
```javascript
socket.emit('stop_session', {
  session_id: 'youth'
});
```

**`stream_audio`**
```javascript
socket.emit('stream_audio', {
  session_id: 'youth',
  audio: audioDataArray  // Uint8Array
});
```

#### Server → Client

**`translation_update`**
```javascript
socket.on('translation_update', (data) => {
  // data = {
  //   session_id: 'main',
  //   original_lang: 'en',
  //   original_text: 'Hello',
  //   translated_text: 'Hola',
  //   is_final: true,
  //   timestamp: '2024-01-15T10:30:00'
  // }
});
```

**`translation_error`**
```javascript
socket.on('translation_error', (data) => {
  // data = {
  //   session_id: 'main',
  //   error: 'Error message',
  //   timestamp: '2024-01-15T10:30:00'
  // }
});
```

---

## 🔐 Security Considerations

- **Local Network Only**: System designed for trusted local networks
- **No Authentication**: Implement authentication for production use
- **HTTPS**: Use reverse proxy (nginx) with SSL for production
- **API Key**: Store Soniox API key securely in `.env` (never commit!)
- **Rate Limiting**: Consider rate limiting for REST endpoints

---

## 📝 License

MIT License - feel free to use and modify for your church or organization.

---

## 🤝 Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Submit a pull request

---

## 💬 Support

For issues or questions:
- Check the [Troubleshooting](#troubleshooting) section
- Review server logs: `tail -f translation_server.log`
- Open an issue on GitHub

---

## 🙏 Acknowledgments

- **Soniox** for real-time speech translation API
- **Flask-SocketIO** for WebSocket support
- **PyAudio** for audio capture

---

**Built with ❤️ for multilingual worship communities**
