"""
Real-Time Bilingual Church Translation System - Server
======================================================
A Flask-SocketIO server that manages multiple simultaneous translation sessions,
integrates with Soniox Real-Time API, and broadcasts translations to connected clients.

Author: Church Translation System
License: MIT
"""

import os
import sys
import time
import json
import queue
import threading
import logging
from datetime import datetime
from typing import Dict, Optional, Any
from collections import defaultdict

import pyaudio
from flask import Flask, render_template, send_from_directory, jsonify, request
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_cors import CORS

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('translation_server.log')
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    """Server configuration"""
    # Soniox API Configuration
    SONIOX_API_KEY = os.getenv('SONIOX_API_KEY', '')
    SONIOX_ENABLED = SONIOX_API_KEY != ''
    
    # Testing Mode (no audio, no Soniox)
    TESTING_MODE = os.getenv('TESTING_MODE', 'false').lower() == 'true'
    
    # Audio Configuration
    AUDIO_RATE = 16000  # Soniox requires 16kHz
    AUDIO_CHUNK = 1024
    AUDIO_CHANNELS = 1
    AUDIO_FORMAT = pyaudio.paInt16
    
    # Server Configuration
    HOST = '0.0.0.0'
    PORT = 5000
    DEBUG = os.getenv('DEBUG', 'false').lower() == 'true'
    
    # Session Configuration
    MAX_SESSIONS = 10
    DEFAULT_SESSION_DURATION = 90  # minutes


# ============================================================================
# SONIOX CLIENT (Real-Time Translation)
# ============================================================================

class SonioxClient:
    """
    Thread-safe client for Soniox Real-Time Speech-to-Text and Translation API.
    Handles audio streaming, receives translations, and broadcasts to clients.
    """
    
    def __init__(self, session_id: str, socketio_instance, testing_mode: bool = False):
        """
        Initialize Soniox client for a specific session.
        
        Args:
            session_id: Unique identifier for this translation session
            socketio_instance: Flask-SocketIO instance for broadcasting
            testing_mode: If True, generates dummy translations instead of using API
        """
        self.session_id = session_id
        self.socketio = socketio_instance
        self.testing_mode = testing_mode
        
        # Thread-safe audio queue
        self.audio_queue = queue.Queue(maxsize=1000)
        
        # Control flags
        self.is_active = False
        self.should_stop = threading.Event()
        
        # Threading
        self.worker_thread: Optional[threading.Thread] = None
        
        # Statistics
        self.bytes_sent = 0
        self.results_received = 0
        
        logger.info(f"[{self.session_id}] SonioxClient initialized (testing_mode={testing_mode})")
    
    def start(self):
        """Start the Soniox client worker thread"""
        if self.is_active:
            logger.warning(f"[{self.session_id}] Already active")
            return
        
        self.is_active = True
        self.should_stop.clear()
        
        if self.testing_mode:
            self.worker_thread = threading.Thread(target=self._testing_worker, daemon=True)
        else:
            self.worker_thread = threading.Thread(target=self._soniox_worker, daemon=True)
        
        self.worker_thread.start()
        logger.info(f"[{self.session_id}] Worker thread started")
    
    def stop(self):
        """Stop the Soniox client gracefully"""
        logger.info(f"[{self.session_id}] Stopping...")
        self.is_active = False
        self.should_stop.set()
        
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=5)
        
        logger.info(f"[{self.session_id}] Stopped. Stats: {self.bytes_sent} bytes sent, {self.results_received} results received")
    
    def add_audio(self, audio_chunk: bytes):
        """
        Add audio chunk to processing queue (thread-safe).
        
        Args:
            audio_chunk: Raw audio bytes (16kHz, 16-bit PCM)
        """
        try:
            self.audio_queue.put_nowait(audio_chunk)
        except queue.Full:
            logger.warning(f"[{self.session_id}] Audio queue full, dropping chunk")
    
    def _testing_worker(self):
        """
        Testing mode worker - generates dummy bilingual translations
        for testing without Soniox API or real audio.
        """
        logger.info(f"[{self.session_id}] Testing worker started")
        
        # Sample phrases for testing
        test_phrases = [
            {
                "original_lang": "en",
                "original_text": "Welcome to our church service today.",
                "translated_text": "Bienvenidos a nuestro servicio de iglesia hoy."
            },
            {
                "original_lang": "en",
                "original_text": "Let us pray together.",
                "translated_text": "Oremos juntos."
            },
            {
                "original_lang": "es",
                "original_text": "La paz de Cristo esté con ustedes.",
                "translated_text": "The peace of Christ be with you."
            },
            {
                "original_lang": "en",
                "original_text": "We gather here in faith and fellowship.",
                "translated_text": "Nos reunimos aquí en fe y compañerismo."
            },
            {
                "original_lang": "es",
                "original_text": "Dios les bendiga a todos.",
                "translated_text": "God bless you all."
            },
            {
                "original_lang": "en",
                "original_text": "Today's scripture reading is from the book of John.",
                "translated_text": "La lectura de las escrituras de hoy es del libro de Juan."
            },
        ]
        
        phrase_index = 0
        word_index = 0
        
        try:
            while not self.should_stop.is_set():
                # Simulate progressive word-by-word updates
                current_phrase = test_phrases[phrase_index]
                original_words = current_phrase["original_text"].split()
                translated_words = current_phrase["translated_text"].split()
                
                # Send partial (non-final) update
                if word_index < len(original_words):
                    partial_original = " ".join(original_words[:word_index + 1])
                    partial_translated = " ".join(translated_words[:min(word_index + 1, len(translated_words))])
                    
                    self._broadcast_translation(
                        original_lang=current_phrase["original_lang"],
                        original_text=partial_original,
                        translated_text=partial_translated,
                        is_final=False
                    )
                    
                    word_index += 1
                    time.sleep(0.3)  # Simulate speech rate
                else:
                    # Send final update
                    self._broadcast_translation(
                        original_lang=current_phrase["original_lang"],
                        original_text=current_phrase["original_text"],
                        translated_text=current_phrase["translated_text"],
                        is_final=True
                    )
                    
                    # Move to next phrase
                    phrase_index = (phrase_index + 1) % len(test_phrases)
                    word_index = 0
                    time.sleep(2.5)  # Pause between phrases
                    
        except Exception as e:
            logger.error(f"[{self.session_id}] Testing worker error: {e}", exc_info=True)
        finally:
            logger.info(f"[{self.session_id}] Testing worker stopped")
    
    def _soniox_worker(self):
        """
        Production worker - connects to Soniox API and processes real audio.
        """
        logger.info(f"[{self.session_id}] Soniox worker started")
        
        if not Config.SONIOX_API_KEY:
            logger.error(f"[{self.session_id}] SONIOX_API_KEY not set!")
            self._broadcast_error("API key not configured")
            return
        
        try:
            import soniox
            from soniox.speech_service import SpeechClient
            from soniox.transcribe_live import transcribe_stream
            
            # Initialize Soniox client
            client = SpeechClient(api_key=Config.SONIOX_API_KEY)
            
            # Configure transcription with translation
            # English to Spanish and Spanish to English
            config = {
                "model": "generic",
                "enable_translation": True,
                "translation_config": {
                    "target_languages": ["es", "en"]  # Translate to both
                },
                "enable_endpoint_detection": True,
                "enable_streaming_speaker_diarization": False,
            }
            
            logger.info(f"[{self.session_id}] Soniox client configured")
            
            # Audio generator from queue
            def audio_generator():
                while not self.should_stop.is_set():
                    try:
                        chunk = self.audio_queue.get(timeout=0.1)
                        self.bytes_sent += len(chunk)
                        yield chunk
                    except queue.Empty:
                        continue
            
            # Process results
            for result in transcribe_stream(client, audio_generator(), **config):
                if self.should_stop.is_set():
                    break
                
                self.results_received += 1
                
                # Extract original and translated text
                original_text = result.get('text', '')
                is_final = result.get('is_final', False)
                detected_lang = result.get('language', 'en')
                
                # Get translation
                translations = result.get('translations', {})
                target_lang = 'es' if detected_lang == 'en' else 'en'
                translated_text = translations.get(target_lang, '')
                
                if original_text:
                    self._broadcast_translation(
                        original_lang=detected_lang,
                        original_text=original_text,
                        translated_text=translated_text,
                        is_final=is_final
                    )
            
        except ImportError:
            logger.error(f"[{self.session_id}] Soniox SDK not installed. Install with: pip install soniox")
            self._broadcast_error("Soniox SDK not installed")
        except Exception as e:
            logger.error(f"[{self.session_id}] Soniox worker error: {e}", exc_info=True)
            self._broadcast_error(f"Translation error: {str(e)}")
        finally:
            logger.info(f"[{self.session_id}] Soniox worker stopped")
    
    def _broadcast_translation(self, original_lang: str, original_text: str, 
                             translated_text: str, is_final: bool):
        """
        Broadcast translation result to all clients in this session's room.
        
        Args:
            original_lang: Language code of original speech ('en' or 'es')
            original_text: Original transcribed text
            translated_text: Translated text
            is_final: Whether this is a final or interim result
        """
        message = {
            "session_id": self.session_id,
            "original_lang": original_lang,
            "original_text": original_text,
            "translated_text": translated_text,
            "is_final": is_final,
            "timestamp": datetime.now().isoformat()
        }
        
        # Broadcast to session room
        self.socketio.emit('translation_update', message, room=self.session_id)
        
        if is_final:
            logger.info(f"[{self.session_id}] {original_lang.upper()}: {original_text[:50]}...")
    
    def _broadcast_error(self, error_message: str):
        """Broadcast error message to session room"""
        self.socketio.emit('translation_error', {
            "session_id": self.session_id,
            "error": error_message,
            "timestamp": datetime.now().isoformat()
        }, room=self.session_id)


# ============================================================================
# AUDIO CAPTURE (Local Microphone/Line-In)
# ============================================================================

class AudioCapture:
    """
    Captures audio from local microphone/line-in and feeds to a SonioxClient.
    Runs in a separate thread for non-blocking operation.
    """
    
    def __init__(self, soniox_client: SonioxClient):
        """
        Initialize audio capture.
        
        Args:
            soniox_client: SonioxClient instance to receive audio data
        """
        self.soniox_client = soniox_client
        self.audio = pyaudio.PyAudio()
        self.stream: Optional[pyaudio.Stream] = None
        self.is_active = False
        self.should_stop = threading.Event()
        self.capture_thread: Optional[threading.Thread] = None
        
        logger.info(f"[{soniox_client.session_id}] AudioCapture initialized")
    
    def start(self):
        """Start audio capture"""
        if self.is_active:
            logger.warning(f"[{self.soniox_client.session_id}] Audio capture already active")
            return
        
        self.is_active = True
        self.should_stop.clear()
        
        try:
            # Open audio stream
            self.stream = self.audio.open(
                format=Config.AUDIO_FORMAT,
                channels=Config.AUDIO_CHANNELS,
                rate=Config.AUDIO_RATE,
                input=True,
                frames_per_buffer=Config.AUDIO_CHUNK,
                stream_callback=self._audio_callback
            )
            
            self.stream.start_stream()
            logger.info(f"[{self.soniox_client.session_id}] Audio capture started")
            
        except Exception as e:
            logger.error(f"[{self.soniox_client.session_id}] Failed to start audio: {e}", exc_info=True)
            self.is_active = False
    
    def stop(self):
        """Stop audio capture"""
        logger.info(f"[{self.soniox_client.session_id}] Stopping audio capture...")
        self.is_active = False
        self.should_stop.set()
        
        if self.stream:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception as e:
                logger.error(f"Error closing stream: {e}")
        
        logger.info(f"[{self.soniox_client.session_id}] Audio capture stopped")
    
    def _audio_callback(self, in_data, frame_count, time_info, status):
        """PyAudio callback - called for each audio chunk"""
        if status:
            logger.warning(f"Audio callback status: {status}")
        
        if self.is_active and in_data:
            self.soniox_client.add_audio(in_data)
        
        return (None, pyaudio.paContinue)
    
    def __del__(self):
        """Cleanup"""
        if hasattr(self, 'audio'):
            self.audio.terminate()


# ============================================================================
# SESSION MANAGER
# ============================================================================

class SessionManager:
    """
    Manages multiple simultaneous translation sessions.
    Each session has its own SonioxClient and optional AudioCapture.
    """
    
    def __init__(self, socketio_instance):
        """
        Initialize session manager.
        
        Args:
            socketio_instance: Flask-SocketIO instance
        """
        self.socketio = socketio_instance
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()
        
        logger.info("SessionManager initialized")
    
    def create_session(self, session_id: str, use_local_audio: bool = False, 
                      testing_mode: bool = False) -> bool:
        """
        Create a new translation session.
        
        Args:
            session_id: Unique identifier for the session
            use_local_audio: If True, capture from local microphone
            testing_mode: If True, use dummy translations
            
        Returns:
            True if session created successfully, False otherwise
        """
        with self.lock:
            if session_id in self.sessions:
                logger.warning(f"Session '{session_id}' already exists")
                return False
            
            if len(self.sessions) >= Config.MAX_SESSIONS:
                logger.error(f"Maximum sessions ({Config.MAX_SESSIONS}) reached")
                return False
            
            try:
                # Create Soniox client
                soniox_client = SonioxClient(
                    session_id=session_id,
                    socketio_instance=self.socketio,
                    testing_mode=testing_mode
                )
                soniox_client.start()
                
                # Create session object
                session = {
                    'session_id': session_id,
                    'soniox_client': soniox_client,
                    'audio_capture': None,
                    'use_local_audio': use_local_audio,
                    'testing_mode': testing_mode,
                    'created_at': datetime.now(),
                    'client_count': 0
                }
                
                # Setup local audio capture if requested
                if use_local_audio and not testing_mode:
                    audio_capture = AudioCapture(soniox_client)
                    audio_capture.start()
                    session['audio_capture'] = audio_capture
                
                self.sessions[session_id] = session
                logger.info(f"Session '{session_id}' created (local_audio={use_local_audio}, testing={testing_mode})")
                return True
                
            except Exception as e:
                logger.error(f"Failed to create session '{session_id}': {e}", exc_info=True)
                return False
    
    def stop_session(self, session_id: str) -> bool:
        """
        Stop and remove a translation session.
        
        Args:
            session_id: Session to stop
            
        Returns:
            True if stopped successfully, False if session not found
        """
        with self.lock:
            if session_id not in self.sessions:
                logger.warning(f"Session '{session_id}' not found")
                return False
            
            session = self.sessions[session_id]
            
            # Stop audio capture if active
            if session['audio_capture']:
                session['audio_capture'].stop()
            
            # Stop Soniox client
            session['soniox_client'].stop()
            
            # Remove session
            del self.sessions[session_id]
            logger.info(f"Session '{session_id}' stopped and removed")
            return True
    
    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session by ID"""
        return self.sessions.get(session_id)
    
    def get_all_sessions(self) -> Dict[str, Any]:
        """Get summary of all active sessions"""
        with self.lock:
            return {
                sid: {
                    'session_id': s['session_id'],
                    'use_local_audio': s['use_local_audio'],
                    'testing_mode': s['testing_mode'],
                    'created_at': s['created_at'].isoformat(),
                    'client_count': s['client_count']
                }
                for sid, s in self.sessions.items()
            }
    
    def increment_client_count(self, session_id: str):
        """Increment connected client count for a session"""
        if session_id in self.sessions:
            self.sessions[session_id]['client_count'] += 1
    
    def decrement_client_count(self, session_id: str):
        """Decrement connected client count for a session"""
        if session_id in self.sessions:
            self.sessions[session_id]['client_count'] = max(0, self.sessions[session_id]['client_count'] - 1)


# ============================================================================
# FLASK APP & SOCKETIO SETUP
# ============================================================================

app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'church-translation-secret-key-change-in-production')
CORS(app)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', 
                    logger=True, engineio_logger=False)

# Initialize session manager
session_manager = SessionManager(socketio)


# ============================================================================
# HTTP ROUTES
# ============================================================================

@app.route('/')
def index():
    """Main landing page"""
    return send_from_directory('static', 'index.html')

@app.route('/projector')
def projector():
    """Projector display client"""
    return send_from_directory('static', 'projector.html')

@app.route('/personal')
def personal():
    """Personal phone client"""
    return send_from_directory('static', 'personal.html')

@app.route('/streamer')
def streamer():
    """Audio streamer client (browser-based audio input)"""
    return send_from_directory('static', 'audio_streamer.html')

@app.route('/api/sessions', methods=['GET'])
def get_sessions():
    """Get list of all active sessions"""
    return jsonify({
        'success': True,
        'sessions': session_manager.get_all_sessions()
    })

@app.route('/api/sessions/<session_id>', methods=['POST'])
def create_session_http(session_id):
    """
    Create a new session via HTTP.
    Body: { "use_local_audio": bool, "testing_mode": bool }
    """
    data = request.get_json() or {}
    use_local_audio = data.get('use_local_audio', False)
    testing_mode = data.get('testing_mode', Config.TESTING_MODE)
    
    success = session_manager.create_session(session_id, use_local_audio, testing_mode)
    
    return jsonify({
        'success': success,
        'session_id': session_id,
        'message': f"Session '{session_id}' {'created' if success else 'failed to create'}"
    }), 201 if success else 400

@app.route('/api/sessions/<session_id>', methods=['DELETE'])
def stop_session_http(session_id):
    """Stop a session via HTTP"""
    success = session_manager.stop_session(session_id)
    
    return jsonify({
        'success': success,
        'session_id': session_id,
        'message': f"Session '{session_id}' {'stopped' if success else 'not found'}"
    }), 200 if success else 404

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'active_sessions': len(session_manager.sessions),
        'testing_mode': Config.TESTING_MODE,
        'soniox_enabled': Config.SONIOX_ENABLED
    })


# ============================================================================
# SOCKETIO EVENT HANDLERS
# ============================================================================

@socketio.on('connect')
def handle_connect():
    """Client connected"""
    logger.info(f"Client connected: {request.sid}")
    emit('connection_response', {'status': 'connected', 'sid': request.sid})

@socketio.on('disconnect')
def handle_disconnect():
    """Client disconnected"""
    logger.info(f"Client disconnected: {request.sid}")

@socketio.on('join_session')
def handle_join_session(data):
    """
    Client requests to join a specific session room.
    Data: { "session_id": "main" }
    """
    session_id = data.get('session_id', 'main')
    
    # Join the session room
    join_room(session_id)
    session_manager.increment_client_count(session_id)
    
    logger.info(f"Client {request.sid} joined session '{session_id}'")
    
    emit('join_response', {
        'success': True,
        'session_id': session_id,
        'message': f"Joined session '{session_id}'"
    })

@socketio.on('leave_session')
def handle_leave_session(data):
    """
    Client leaves a session room.
    Data: { "session_id": "main" }
    """
    session_id = data.get('session_id', 'main')
    
    leave_room(session_id)
    session_manager.decrement_client_count(session_id)
    
    logger.info(f"Client {request.sid} left session '{session_id}'")
    
    emit('leave_response', {
        'success': True,
        'session_id': session_id,
        'message': f"Left session '{session_id}'"
    })

@socketio.on('start_session')
def handle_start_session(data):
    """
    Client requests to start a new session.
    Data: { 
        "session_id": "youth", 
        "use_local_audio": false,
        "testing_mode": false 
    }
    """
    session_id = data.get('session_id', 'main')
    use_local_audio = data.get('use_local_audio', False)
    testing_mode = data.get('testing_mode', Config.TESTING_MODE)
    
    success = session_manager.create_session(session_id, use_local_audio, testing_mode)
    
    emit('start_session_response', {
        'success': success,
        'session_id': session_id,
        'message': f"Session '{session_id}' {'created' if success else 'failed'}"
    })

@socketio.on('stop_session')
def handle_stop_session(data):
    """
    Client requests to stop a session.
    Data: { "session_id": "youth" }
    """
    session_id = data.get('session_id')
    
    if not session_id:
        emit('stop_session_response', {
            'success': False,
            'message': 'session_id required'
        })
        return
    
    success = session_manager.stop_session(session_id)
    
    emit('stop_session_response', {
        'success': success,
        'session_id': session_id,
        'message': f"Session '{session_id}' {'stopped' if success else 'not found'}"
    })

@socketio.on('stream_audio')
def handle_stream_audio(data):
    """
    Client streams audio data to a specific session.
    Data: { 
        "session_id": "youth", 
        "audio": <binary_data_base64_or_arraybuffer> 
    }
    """
    session_id = data.get('session_id')
    audio_data = data.get('audio')
    
    if not session_id or not audio_data:
        logger.warning("stream_audio: missing session_id or audio data")
        return
    
    session = session_manager.get_session(session_id)
    if not session:
        logger.warning(f"stream_audio: session '{session_id}' not found")
        return
    
    # Handle different audio data formats
    if isinstance(audio_data, str):
        # Base64 encoded
        import base64
        try:
            audio_bytes = base64.b64decode(audio_data)
        except Exception as e:
            logger.error(f"Failed to decode audio: {e}")
            return
    else:
        # Assume it's already bytes
        audio_bytes = bytes(audio_data)
    
    # Feed audio to Soniox client
    session['soniox_client'].add_audio(audio_bytes)


# ============================================================================
# STARTUP & INITIALIZATION
# ============================================================================

def start_main_session(duration_minutes: int = 90):
    """
    Start the main meeting session with local audio capture.
    This is the primary session for the main church service.
    
    Args:
        duration_minutes: Expected duration (for logging/scheduling)
    """
    logger.info(f"Starting MAIN session (duration: {duration_minutes} min)")
    
    success = session_manager.create_session(
        session_id='main',
        use_local_audio=not Config.TESTING_MODE,  # Use local audio unless in testing mode
        testing_mode=Config.TESTING_MODE
    )
    
    if success:
        logger.info("MAIN session started successfully")
    else:
        logger.error("Failed to start MAIN session")
    
    return success


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

if __name__ == '__main__':
    logger.info("=" * 70)
    logger.info("Church Translation System - Server Starting")
    logger.info("=" * 70)
    logger.info(f"Testing Mode: {Config.TESTING_MODE}")
    logger.info(f"Soniox Enabled: {Config.SONIOX_ENABLED}")
    logger.info(f"Host: {Config.HOST}:{Config.PORT}")
    logger.info("=" * 70)
    
    # Auto-start main session
    if Config.TESTING_MODE or Config.SONIOX_ENABLED:
        start_main_session(duration_minutes=90)
    else:
        logger.warning("Soniox API key not set. Set SONIOX_API_KEY or enable TESTING_MODE=true")
    
    # Run the server
    try:
        socketio.run(
            app,
            host=Config.HOST,
            port=Config.PORT,
            debug=Config.DEBUG,
            use_reloader=False  # Disable reloader to prevent duplicate sessions
        )
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        # Stop all sessions
        for session_id in list(session_manager.sessions.keys()):
            session_manager.stop_session(session_id)
        logger.info("Server stopped")