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
import logging.handlers
import signal
from datetime import datetime, timedelta
from typing import Dict, Optional, Any
from collections import defaultdict

import pyaudio
from flask import Flask, render_template, send_from_directory, jsonify, request, redirect
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_cors import CORS

from dotenv import load_dotenv
import re

# Load the .env file
load_dotenv()

# Configure logging. Use DEBUG level only when DEBUG env var is true; otherwise reduce noise.
log_level = logging.DEBUG if os.getenv('DEBUG', 'false').lower() == 'true' else logging.WARNING
_log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
_rotating_handler = logging.handlers.RotatingFileHandler(
    'translation_server.log',
    maxBytes=5 * 1024 * 1024,  # 5 MB per file
    backupCount=3               # Keep 3 backups = 15 MB max on disk
)
_rotating_handler.setFormatter(logging.Formatter(_log_format))
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter(_log_format))
logging.basicConfig(level=log_level, handlers=[_stream_handler, _rotating_handler])
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
    AUDIO_RATE = 16000 # Soniox requires 16kHz
    AUDIO_CHUNK = 1024
    AUDIO_CHANNELS = 1
    AUDIO_FORMAT = pyaudio.paInt16
    
    # Server Configuration
    HOST = '0.0.0.0'
    PORT = 5000        # HTTP - used by projector, personal, index
    PORT_HTTPS = 5443  # HTTPS - used by audio streamer (requires microphone)
    DEBUG = os.getenv('DEBUG', 'false').lower() == 'true'
    
    # Session Configuration
    MAX_SESSIONS = 10
    DEFAULT_SESSION_DURATION = 90  # minutes
    MAX_SESSION_HOURS = 8          # Auto-stop sessions older than this (prevents runaway memory)

    # Translation Language Configuration
    # Language A is always English. Language B is configurable.
    LANGUAGE_A = 'en'
    LANGUAGE_B = os.getenv('LANGUAGE_B', 'es')  # Default: Spanish

    # Supported translation languages (Soniox 2-letter codes)
    SUPPORTED_LANGUAGES = ['zh', 'en', 'fr', 'it', 'ko', 'pt', 'es', 'vi','nl']


# ============================================================================
# SONIOX CLIENT (Real-Time Translation)
# ============================================================================

class SonioxClient:
    """
    Thread-safe client for Soniox Real-Time Speech-to-Text and Translation API.
    Handles audio streaming, receives translations, and broadcasts to clients.
    """
    
    def __init__(self, session_id: str, socketio_instance, testing_mode: bool = False,
                 language_b: str = 'es'):
        """
        Initialize Soniox client for a specific session.
        
        Args:
            session_id: Unique identifier for this translation session
            socketio_instance: Flask-SocketIO instance for broadcasting
            testing_mode: If True, generates dummy translations instead of using API
            language_b: The non-English translation language (e.g. 'es', 'zh', 'fr')
        """
        self.session_id = session_id
        self.socketio = socketio_instance
        self.testing_mode = testing_mode
        self.language_b = language_b
        
        # Thread-safe audio queue
        self.audio_queue = queue.Queue(maxsize=1000)
        
        # Control flags
        self.is_active = False
        self.should_stop = threading.Event()
        
        # Threading
        self.worker_thread: Optional[threading.Thread] = None
        self._watchdog_timer: Optional[threading.Timer] = None
        
        # Statistics
        self.bytes_sent = 0
        self.results_received = 0
        
        logger.info(f"[{self.session_id}] SonioxClient initialized (testing_mode={testing_mode}, language_b={language_b})")
    
    def start(self):
        """Start the Soniox client worker thread"""
        if self.is_active:
            logger.warning(f"[{self.session_id}] Already active")
            return
        
        self.is_active = True
        self.should_stop.clear()
        
        if self.testing_mode:
            self.worker_thread = threading.Thread(
                target=self._testing_worker, daemon=True,
                name=f"soniox-test-{self.session_id}")
        else:
            self.worker_thread = threading.Thread(
                target=self._soniox_worker, daemon=True,
                name=f"soniox-worker-{self.session_id}")
        
        self.worker_thread.start()
        logger.info(f"[{self.session_id}] Worker thread started")
        self._schedule_watchdog()
    
    def stop(self):
        """Stop the Soniox client gracefully"""
        logger.info(f"[{self.session_id}] Stopping...")
        self.is_active = False
        self.should_stop.set()

        # Cancel watchdog so it doesn't try to restart us after we stop
        if self._watchdog_timer:
            self._watchdog_timer.cancel()
            self._watchdog_timer = None
        
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=5)
        
        logger.info(f"[{self.session_id}] Stopped. Stats: {self.bytes_sent} bytes sent, {self.results_received} results received")

    # -----------------------------------------------------------------------
    # Watchdog — detects dead worker thread and restarts it
    # -----------------------------------------------------------------------
    def _schedule_watchdog(self, interval: int = 30):
        """Schedule a watchdog check after `interval` seconds."""
        if self.should_stop.is_set():
            return
        self._watchdog_timer = threading.Timer(interval, self._watchdog_check)
        self._watchdog_timer.daemon = True
        self._watchdog_timer.start()

    def _watchdog_check(self):
        """Restart the worker thread if it died unexpectedly."""
        if self.should_stop.is_set():
            return
        if self.worker_thread and not self.worker_thread.is_alive():
            logger.warning(f"[{self.session_id}] Worker thread died unexpectedly — restarting")
            self._broadcast_error("Translation worker restarting after unexpected failure")
            # Re-spawn the right worker
            if self.testing_mode:
                self.worker_thread = threading.Thread(
                    target=self._testing_worker, daemon=True,
                    name=f"soniox-test-{self.session_id}")
            else:
                self.worker_thread = threading.Thread(
                    target=self._soniox_worker, daemon=True,
                    name=f"soniox-worker-{self.session_id}")
            self.worker_thread.start()
            logger.info(f"[{self.session_id}] Worker thread restarted by watchdog")
        # Re-schedule the next check
        self._schedule_watchdog()
    
    def add_audio(self, audio_chunk: bytes):
        """
        Add audio chunk to processing queue (thread-safe).
        
        Args:
            audio_chunk: Raw audio bytes (16kHz, 16-bit PCM)
        """
        try:
            # If the queue is full, we must be lagging. Discard oldest chunk.
            self.audio_queue.put_nowait(audio_chunk)
        except queue.Full:
            try:
                self.audio_queue.get_nowait()
                self.audio_queue.put_nowait(audio_chunk)
                logger.warning(f"[{self.session_id}] Audio queue full, dropping oldest chunk")
            except queue.Empty:
                pass
    
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
        Production worker - connects to Soniox WebSocket API and processes real audio.
        """
        logger.info(f"[{self.session_id}] Soniox worker started")
        
        if not Config.SONIOX_API_KEY:
            logger.error(f"[{self.session_id}] SONIOX_API_KEY not set!")
            if hasattr(self, '_broadcast_error'):
                self._broadcast_error("API key not configured")
            return
        
        try:
            from websockets.sync.client import connect
            from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError
        except ImportError as e:
            logger.error(f"[{self.session_id}] Missing required library: {e}")
            logger.error(f"[{self.session_id}] Install with: pip install websockets")
            if hasattr(self, '_broadcast_error'):
                self._broadcast_error("WebSocket library not installed")
            return
            
        SONIOX_WEBSOCKET_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
        
        while not self.should_stop.is_set():
            try:
                # Configure transcription with translation
                # Two-way translation: English <-> Spanish
                config = {
                    "api_key": Config.SONIOX_API_KEY,
                    "model": "stt-rt-v4",
                    
                    # Enable language identification and hints
                    "language_hints": [Config.LANGUAGE_A, self.language_b],
                    "enable_language_identification": True,
                    
                    # Two-way translation between English and the configured language
                    "translation": {
                        "type": "two_way",
                        "language_a": Config.LANGUAGE_A,
                        "language_b": self.language_b,
                    },
                    
                    # Audio format - raw PCM 16-bit
                    "audio_format": "pcm_s16le",
                    "sample_rate": Config.AUDIO_RATE,
                    "num_channels": Config.AUDIO_CHANNELS,
                    
                    "context": {
                        "general": [
                            {"key": "organization", "value": "LDS Church"},
                        ],
                    "text": "These are lessons and sermons for the Church of Jesus Christ of Latter-day Saints. The content includes religious teachings, scriptures, and guidance for members of the church. The tone is spiritual, reverent and sometimes humorous, but never crude or vulgar. People may talk about hell or damnation in a religious context.",
                    "terms": [
                        "priesthood",
                        "prophet",
                        "Mormon",
                        "Latter-day",
                        "Moroni",
                        "Nephi",
                        "Alma",
                        "Mosiah",
                        "Lamanites",
                        "Nephites"
                    ],
                    "translation_terms": [
                        # English -> Spanish
                        {"source": "Stake", "target": "Estaca"},
                        {"source": "Ward", "target": "Barrio"},
                        {"source": "Ward Council", "target": "Consejo de barrio"},
                        {"source": "Stake Council", "target": "Consejo de estaca"},
                        {"source": "Branch", "target": "Rama"},
                        {"source": "District", "target": "Distrito"},
                        {"source": "Sacrament", "target": "Santa Cena"},
                        {"source": "Sacrament Meeting", "target": "Reunión Sacramental"},
                        {"source": "Relief Society", "target": "Sociedad de Socorro"},
                        {"source": "Elders Quorum", "target": "Quórum de élderes"},
                        {"source": "Primary", "target": "Primaria"},
                        {"source": "Sunday School", "target": "Escuela Dominical"},
                        {"source": "Young Men", "target": "Hombres Jóvenes"},
                        {"source": "Young Women", "target": "Mujeres Jóvenes"},
                        {"source": "Bishop", "target": "Obispo"},
                        {"source": "Bishopric", "target": "Obispado"},
                        {"source": "Stake President", "target": "Presidente de estaca"},
                        {"source": "High Council", "target": "Sumo Consejo"},
                        {"source": "Priesthood", "target": "Sacerdocio"},
                        {"source": "Aaronic Priesthood", "target": "Sacerdocio Aarónico"},
                        {"source": "Melchizedek Priesthood", "target": "Sacerdocio de Melquisedec"},
                        {"source": "Deacon", "target": "Diácono"},
                        {"source": "Teacher", "target": "Maestro"},
                        {"source": "Priest", "target": "Presbítero"},
                        {"source": "Elder", "target": "Élder"},
                        {"source": "High Priest", "target": "Sumo Sacerdote"},
                        {"source": "Seventy", "target": "Setenta"},
                        {"source": "Apostle", "target": "Apóstol"},
                        {"source": "Prophet", "target": "Profeta"},
                        {"source": "General Conference", "target": "Conferencia General"},
                        {"source": "Endowment", "target": "Investidura"},
                        {"source": "Sealing", "target": "Sellamiento"},
                        {"source": "Tithing", "target": "Diezmo"},
                        {"source": "Fast Offering", "target": "Ofrenda de ayuno"},
                        {"source": "Fast Sunday", "target": "Domingo de ayuno"},
                        {"source": "Testimony", "target": "Testimonio"},
                        {"source": "Testimony Meeting", "target": "Reunión de testimonios"},
                        {"source": "Calling", "target": "Llamamiento"},
                        {"source": "Release", "target": "Relevo"},
                        {"source": "Sustaining", "target": "Sostenimiento"},
                        {"source": "Set apart", "target": "Apartar"},
                        {"source": "Gospel", "target": "Evangelio"},
                        {"source": "Atonement", "target": "Expiación"},
                        {"source": "Covenant", "target": "Convenio"},
                        {"source": "Ordinance", "target": "Ordenanza"},
                        {"source": "Temple", "target": "Templo"},
                        {"source": "Meetinghouse", "target": "Centro de reuniones"},
                        {"source": "Chapel", "target": "Capilla"},
                        {"source": "Family History", "target": "Historia Familiar"},
                        {"source": "Missionary", "target": "Misionero"},
                        {"source": "Seminary", "target": "Seminario"},
                        {"source": "Institute", "target": "Instituto"},
                        {"source": "Book of Mormon", "target": "Libro de Mormón"},
                        {"source": "Doctrine and Covenants", "target": "Doctrina y Convenios"},
                        {"source": "Pearl of Great Price", "target": "Perla de Gran Precio"},
                        {"source": "Come, Follow Me", "target": "Ven, sígueme"},
                        {"source": "Ministering", "target": "Ministración"},
                        {"source": "Patriarchal Blessing", "target": "Bendición Patriarcal"},
                        
                        # Spanish -> English
                        {"source": "Estaca", "target": "Stake"},
                        {"source": "Barrio", "target": "Ward"},
                        {"source": "Consejo de barrio", "target": "Ward Council"},
                        {"source": "Consejo de estaca", "target": "Stake Council"},
                        {"source": "Rama", "target": "Branch"},
                        {"source": "Distrito", "target": "District"},
                        {"source": "Santa Cena", "target": "Sacrament"},
                        {"source": "Reunión Sacramental", "target": "Sacrament Meeting"},
                        {"source": "Sociedad de Socorro", "target": "Relief Society"},
                        {"source": "Quórum de élderes", "target": "Elders Quorum"},
                        {"source": "Primaria", "target": "Primary"},
                        {"source": "Escuela Dominical", "target": "Sunday School"},
                        {"source": "Hombres Jóvenes", "target": "Young Men"},
                        {"source": "Mujeres Jóvenes", "target": "Young Women"},
                        {"source": "Obispo", "target": "Bishop"},
                        {"source": "Obispado", "target": "Bishopric"},
                        {"source": "Presidente de estaca", "target": "Stake President"},
                        {"source": "Sumo Consejo", "target": "High Council"},
                        {"source": "Sacerdocio", "target": "Priesthood"},
                        {"source": "Sacerdocio Aarónico", "target": "Aaronic Priesthood"},
                        {"source": "Sacerdocio de Melquisedec", "target": "Melchizedek Priesthood"},
                        {"source": "Diácono", "target": "Deacon"},
                        {"source": "Maestro", "target": "Teacher"},
                        {"source": "Presbítero", "target": "Priest"},
                        {"source": "Élder", "target": "Elder"},
                        {"source": "Sumo Sacerdote", "target": "High Priest"},
                        {"source": "Setenta", "target": "Seventy"},
                        {"source": "Apóstol", "target": "Apostle"},
                        {"source": "Profeta", "target": "Prophet"},
                        {"source": "Conferencia General", "target": "General Conference"},
                        {"source": "Investidura", "target": "Endowment"},
                        {"source": "Sellamiento", "target": "Sealing"},
                        {"source": "Diezmo", "target": "Tithing"},
                        {"source": "Ofrenda de ayuno", "target": "Fast Offering"},
                        {"source": "Domingo de ayuno", "target": "Fast Sunday"},
                        {"source": "Testimonio", "target": "Testimony"},
                        {"source": "Reunión de testimonios", "target": "Testimony Meeting"},
                        {"source": "Llamamiento", "target": "Calling"},
                        {"source": "Relevo", "target": "Release"},
                        {"source": "Sostenimiento", "target": "Sustaining"},
                        {"source": "Apartar", "target": "Set apart"},
                        {"source": "Evangelio", "target": "Gospel"},
                        {"source": "Expiación", "target": "Atonement"},
                        {"source": "Convenio", "target": "Covenant"},
                        {"source": "Ordenanza", "target": "Ordinance"},
                        {"source": "Templo", "target": "Temple"},
                        {"source": "Centro de reuniones", "target": "Meetinghouse"},
                        {"source": "Capilla", "target": "Chapel"},
                        {"source": "Historia Familiar", "target": "Family History"},
                        {"source": "Misionero", "target": "Missionary"},
                        {"source": "Seminario", "target": "Seminary"},
                        {"source": "Instituto", "target": "Institute"},
                        {"source": "Libro de Mormón", "target": "Book of Mormon"},
                        {"source": "Doctrina y Convenios", "target": "Doctrine and Covenants"},
                        {"source": "Perla de Gran Precio", "target": "Pearl of Great Price"},
                        {"source": "Ven, sígueme", "target": "Come, Follow Me"},
                        {"source": "Ministración", "target": "Ministering"},
                        {"source": "Bendición Patriarcal", "target": "Patriarchal Blessing"}
                        ]
                    }
                }
                
                # Connect to WebSocket (with explicit timeouts for stuck network robustness)
                with connect(SONIOX_WEBSOCKET_URL, open_timeout=10, close_timeout=5) as ws:
                    logger.info(f"[{self.session_id}] Connected to Soniox API")
                    
                    # Send configuration first
                    ws.send(json.dumps(config))
                    
                    # Start audio streaming thread
                    def stream_audio():
                        try:
                            while not self.should_stop.is_set():
                                try:
                                    chunk = self.audio_queue.get(timeout=0.1)
                                    if chunk:
                                        try:
                                            ws.send(chunk)
                                            self.bytes_sent += len(chunk)
                                        except (ConnectionClosedOK, ConnectionClosedError) as e:
                                            logger.info(f"[{self.session_id}] WebSocket closed while sending audio: {e}")
                                            # Stop further attempts to send
                                            self.should_stop.set()
                                            break
                                except queue.Empty:
                                    continue
                        except Exception as e:
                            # Only log unexpected errors
                            logger.error(f"[{self.session_id}] Audio streaming error: {e}", exc_info=True)
                        finally:
                            # Send empty string to signal end of audio
                            try:
                                if not getattr(ws, 'closed', False):
                                    ws.send("")
                                    logger.info(f"[{self.session_id}] Sent end-of-audio signal")
                            except Exception:
                                # Ignore errors here; connection may already be closed
                                pass
                    
                    audio_thread = threading.Thread(target=stream_audio, daemon=True)
                    audio_thread.start()
                    
                    # Process responses
                    # Track tokens by language for aggregation
                    current_language = None
                    last_final_orig = None
                    last_final_trans = None
                
                    try:
                        while not self.should_stop.is_set():
                            try:
                                message = ws.recv(timeout=0.1)
                                response = json.loads(message)
                                
                                # Check for errors
                                if response.get('error_code') is not None:
                                    error_msg = f"{response['error_code']}: {response.get('error_message', 'Unknown error')}"
                                    logger.error(f"[{self.session_id}] Soniox error: {error_msg}")
                                    if hasattr(self, '_broadcast_error'):
                                        self._broadcast_error(error_msg)
                                    break
                                # Process tokens
                                tokens = response.get('tokens', [])
                                if tokens:
                                    self.results_received += 1
                                    
                                    # Separate original and translated tokens
                                    original_tokens = []
                                    translated_tokens = []
                                    
                                    logger.info(f"[{self.session_id}] Tokens: {tokens}")
                                    for token in tokens:
                                        text = token.get('text', '')
                                        is_final = token.get('is_final', False)
                                        language = token.get('language', 'en')
                                        translation_status = token.get('translation_status', 'none')
                                        
                                        if not text or text in ['<end>', '<fin>']:
                                            continue
                                        
                                        if translation_status == 'translation':
                                            translated_tokens.append(token)
                                        elif translation_status == 'original' or translation_status == 'none':
                                            original_tokens.append(token)
                                            if language:
                                                current_language = language
                                    
                                    # Determine if the phrase is fully final
                                    has_orig_final = any(t.get('is_final') for t in original_tokens)
                                    has_trans_final = any(t.get('is_final') for t in translated_tokens)
                                    
                                    is_fully_final = False
                                    if original_tokens and translated_tokens:
                                        is_fully_final = has_orig_final and has_trans_final
                                    elif original_tokens:
                                        is_fully_final = has_orig_final
                                    elif translated_tokens:
                                        is_fully_final = has_trans_final
                                    
                                    # Build text from tokens by concatenating token texts
                                    # (tokens may already contain leading/trailing spaces).
                                    original_text = ''.join(t.get('text', '') for t in original_tokens if t.get('text'))
                                    translated_text = ''.join(t.get('text', '') for t in translated_tokens if t.get('text'))
    
                                    # Clean up common spacing/tokenization artifacts:
                                    def clean_text(s: str) -> str:
                                        if not s:
                                            return s
                                        #remove all vulgarities
                                        s = re.sub(r'\b(fuck|shit|damn|bastard|penis|ass|bitch|dick|piss)\b', '#$%!', s, flags=re.IGNORECASE)
                                        #remove all spanish vulgarities
                                        s = re.sub(r'\b(joder|mierda|maldita sea|cabron|pene|culo|perra|polla|meada)\b', '#$%!', s, flags=re.IGNORECASE)
                                        return s.strip()
    
                                    original_text = clean_text(original_text)
                                    translated_text = clean_text(translated_text)
                                    
                                    # Broadcast if we have text
                                    if original_text or translated_text:
                                        if is_fully_final:
                                            if last_final_orig == original_text and last_final_trans == translated_text:
                                                pass # Skip identical consecutive final broadcasts to prevent duplicate history
                                            else:
                                                last_final_orig = original_text
                                                last_final_trans = translated_text
                                                self._broadcast_translation(
                                                    original_lang=current_language or 'en',
                                                    original_text=original_text,
                                                    translated_text=translated_text,
                                                    is_final=True
                                                )
                                        else:
                                            # If we broadcast a non-final text, a new phrase is building
                                            last_final_orig = None
                                            last_final_trans = None
                                            self._broadcast_translation(
                                                original_lang=current_language or 'en',
                                                original_text=original_text,
                                                translated_text=translated_text,
                                                is_final=False
                                            )
                                
                                # Check if session is finished
                                if response.get('finished'):
                                    logger.info(f"[{self.session_id}] Soniox session finished")
                                    break
                                    
                            except TimeoutError:
                                        # Normal timeout, continue loop
                                        continue
                    except ConnectionClosedOK:
                        logger.info(f"[{self.session_id}] Soniox connection closed normally. Restarting...")
                    except ConnectionClosedError as e:
                        logger.error(f"[{self.session_id}] Soniox connection closed with error: {e}. Reconnecting...")
                        if hasattr(self, '_broadcast_error'):
                            self._broadcast_error(f"Connection error: {e}. Reconnecting...")
                    except Exception as e:
                        logger.error(f"[{self.session_id}] Soniox connection exception: {e}")
                        
                    # Wait for audio thread to finish
                    audio_thread.join(timeout=2)
                    
                    if not self.should_stop.is_set():
                        # Briefly wait before attempting to reconnect
                        logger.info(f"[{self.session_id}] Waiting 2 seconds before reconnecting...")
                        time.sleep(2)
                    
            except Exception as e:
                logger.error(f"[{self.session_id}] Soniox worker error: {e}", exc_info=True)
                if hasattr(self, '_broadcast_error'):
                    self._broadcast_error(f"Translation error: {str(e)}")
                # If we get here it means we couldn't even connect. Backoff and retry.
                if not self.should_stop.is_set():
                    time.sleep(5)
    
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
        
        try:
            # Broadcast to session room
            self.socketio.emit('translation_update', message, room=self.session_id)
        except Exception as e:
            logger.error(f"[{self.session_id}] Error broadcasting translation: {e}")
        
        #if is_final:
        #    logger.info(f"[{self.session_id}] {original_lang.upper()}: {original_text[:50]}...")
    
    def _broadcast_error(self, error_message: str):
        """Broadcast error message to session room"""
        try:
            self.socketio.emit('translation_error', {
                "session_id": self.session_id,
                "error": error_message,
                "timestamp": datetime.now().isoformat()
            }, room=self.session_id)
        except Exception as e:
            logger.error(f"[{self.session_id}] Error broadcasting error: {e}")


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
    
    def start(self, retries: int = 2) -> bool:
        """Start audio capture, retrying up to `retries` times on failure."""
        if self.is_active:
            logger.warning(f"[{self.soniox_client.session_id}] Audio capture already active")
            return True
        
        self.is_active = True
        self.should_stop.clear()
        
        for attempt in range(retries):
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
                logger.info(f"[{self.soniox_client.session_id}] Audio capture started (attempt {attempt + 1})")
                return True
            except Exception as e:
                logger.warning(
                    f"[{self.soniox_client.session_id}] Audio start attempt {attempt + 1} failed: {e}"
                )
                if attempt < retries - 1:
                    logger.info(f"[{self.soniox_client.session_id}] Retrying audio in 2s...")
                    time.sleep(2)
        
        logger.error(f"[{self.soniox_client.session_id}] All audio start attempts failed")
        self.is_active = False
        return False
    
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
                      testing_mode: bool = False, language_b: Optional[str] = None) -> bool:
        """
        Create a new translation session.
        
        Args:
            session_id: Unique identifier for the session
            use_local_audio: If True, capture from local microphone
            testing_mode: If True, use dummy translations
            language_b: Translation language (defaults to Config.LANGUAGE_B)
            
        Returns:
            True if session created successfully, False otherwise
        """
        if language_b is None:
            language_b = Config.LANGUAGE_B

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
                    testing_mode=testing_mode,
                    language_b=language_b
                )
                soniox_client.start()
                
                # Create session object
                session = {
                    'session_id': session_id,
                    'soniox_client': soniox_client,
                    'audio_capture': None,
                    'use_local_audio': use_local_audio,
                    'testing_mode': testing_mode,
                    'language_b': language_b,
                    'created_at': datetime.now(),
                    'client_count': 0
                }
                
                # Setup local audio capture if requested
                if use_local_audio and not testing_mode:
                    audio_capture = AudioCapture(soniox_client)
                    if not audio_capture.start():
                        logger.error(f"Audio capture failed for session '{session_id}'. Aborting creation.")
                        soniox_client.stop()
                        return False
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
            
            session = self.sessions.pop(session_id)
            
        # Outside the lock, clean up threads so we don't block /health requests for 5 seconds
        # Stop audio capture if active
        if session['audio_capture']:
            session['audio_capture'].stop()
        
        # Stop Soniox client
        session['soniox_client'].stop()
        
        # Notify clients that the session has ended
        try:
            self.socketio.emit('session_stopped', {'session_id': session_id}, room=session_id)
        except Exception as e:
            logger.error(f"Error broadcasting session_stopped: {e}")

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
                    'language_b': s.get('language_b', Config.LANGUAGE_B),
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

    def purge_expired_sessions(self):
        """Stop any sessions older than MAX_SESSION_HOURS (called by background monitor)."""
        cutoff = datetime.now() - timedelta(hours=Config.MAX_SESSION_HOURS)
        expired = [
            sid for sid, s in list(self.sessions.items())
            if s['created_at'] < cutoff
        ]
        for sid in expired:
            logger.warning(f"Session '{sid}' exceeded {Config.MAX_SESSION_HOURS}h max age — auto-stopping")
            self.stop_session(sid)


# ============================================================================
# SSL CERTIFICATE (Self-Signed for Local HTTPS)
# ============================================================================

def ensure_ssl_cert(cert_file: str = 'cert.pem', key_file: str = 'key.pem'):
    """
    Ensure a self-signed SSL certificate exists for HTTPS.
    Generates one automatically on first run using pyOpenSSL.
    The cert is valid for 10 years and covers all IP/hostname combinations.

    Returns:
        Tuple (cert_file, key_file) paths, or (None, None) if pyOpenSSL unavailable.
    """
    if os.path.exists(cert_file) and os.path.exists(key_file):
        logger.info(f"SSL: Using existing cert at '{cert_file}'")
        return cert_file, key_file

    try:
        from OpenSSL import crypto
        import socket as _socket

        logger.info("SSL: Generating new self-signed certificate...")

        # Create key pair
        k = crypto.PKey()
        k.generate_key(crypto.TYPE_RSA, 2048)

        # Create self-signed cert
        cert = crypto.X509()
        cert.get_subject().CN = _socket.gethostname()
        cert.set_serial_number(1)
        cert.gmtime_adj_notBefore(0)
        cert.gmtime_adj_notAfter(10 * 365 * 24 * 60 * 60)  # 10 years
        cert.set_issuer(cert.get_subject())
        cert.set_pubkey(k)

        # Add Subject Alternative Names so the cert matches by IP as well
        try:
            local_ip = _socket.gethostbyname(_socket.gethostname())
        except Exception:
            local_ip = '127.0.0.1'

        san = f'DNS:localhost,DNS:{_socket.gethostname()},IP:127.0.0.1,IP:{local_ip}'
        cert.add_extensions([
            crypto.X509Extension(b'subjectAltName', False, san.encode())
        ])

        cert.sign(k, 'sha256')

        # Write cert and key to disk
        with open(cert_file, 'wb') as f:
            f.write(crypto.dump_certificate(crypto.FILETYPE_PEM, cert))
        with open(key_file, 'wb') as f:
            f.write(crypto.dump_privatekey(crypto.FILETYPE_PEM, k))

        logger.info(f"SSL: Self-signed certificate generated (SAN: {san})")
        return cert_file, key_file

    except ImportError:
        logger.warning("SSL: pyOpenSSL not installed. Running over plain HTTP.")
        logger.warning("SSL: Install with: pip install pyOpenSSL")
        return None, None
    except Exception as e:
        logger.error(f"SSL: Failed to generate certificate: {e}")
        return None, None


# ============================================================================
# FLASK APP & SOCKETIO SETUP
# ============================================================================

app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'church-translation-secret-key-change-in-production')
CORS(app)

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='threading',
    logger=Config.DEBUG,
    engineio_logger=Config.DEBUG
)

# When not debugging, reduce verbosity of the socketio/engineio loggers
if not Config.DEBUG:
    logging.getLogger('socketio.server').setLevel(logging.WARNING)
    logging.getLogger('engineio.server').setLevel(logging.WARNING)

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
    """
    Audio streamer client (browser-based audio input).
    Requires HTTPS for microphone access - redirect HTTP requests to HTTPS port.
    """
    # If request came over plain HTTP, redirect to the HTTPS port
    if request.scheme == 'http':
        https_url = request.url.replace('http://', 'https://', 1)
        # Replace the HTTP port with the HTTPS port
        https_url = https_url.replace(
            f':{Config.PORT}/', f':{Config.PORT_HTTPS}/', 1
        )
        return redirect(https_url, code=302)
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
    data = request.get_json(silent=True) or {}
    use_local_audio = data.get('use_local_audio', False)
    testing_mode = data.get('testing_mode', Config.TESTING_MODE)
    language_b = str(data.get('language_b', Config.LANGUAGE_B))
    
    success = session_manager.create_session(session_id, use_local_audio, testing_mode, language_b)
    
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


@app.route('/api/config', methods=['GET', 'POST'])
def config_endpoint():
    """
    GET: Return current translation language configuration.
    POST: Update the active translation language_b for new sessions.
    Body (POST): { "language_b": "fr" }
    """
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        lang = data.get('language_b', '').strip().lower()
        if lang not in Config.SUPPORTED_LANGUAGES:
            return jsonify({
                'success': False,
                'error': f"Unsupported language '{lang}'. Supported: {Config.SUPPORTED_LANGUAGES}"
            }), 400
        Config.LANGUAGE_B = lang
        logger.info(f"Language B updated to '{lang}'")
        return jsonify({'success': True, 'language_b': Config.LANGUAGE_B})

    # GET
    return jsonify({
        'language_a': Config.LANGUAGE_A,
        'language_b': Config.LANGUAGE_B,
        'supported_languages': Config.SUPPORTED_LANGUAGES
    })

@app.route('/health')
def health():
    """Health check endpoint"""
    session_details = {
        sid: {'client_count': s['client_count']}
        for sid, s in session_manager.sessions.items()
    }
    return jsonify({
        'status': 'healthy',
        'active_sessions': len(session_manager.sessions),
        'session_names': list(session_manager.sessions.keys()),
        'session_details': session_details,
        'language_b': Config.LANGUAGE_B,
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
    """Client disconnected — clean up room membership and client counts."""
    sid = request.sid
    logger.info(f"Client disconnected: {sid}")
    try:
        # rooms() returns the rooms this SID was in at disconnect time.
        # Every client also has a default room equal to its own SID; skip that one.
        for room in list(socketio.server.rooms(sid, '/')):
            if room != sid:
                session_manager.decrement_client_count(room)
    except Exception as e:
        logger.error(f"Error handling disconnect cleanup for {sid}: {e}")

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
    logger.warning("=" * 70)
    logger.warning("Church Translation System - Server Starting")
    logger.warning("=" * 70)
    logger.warning(f"Testing Mode: {Config.TESTING_MODE}")
    logger.warning(f"Soniox Enabled: {Config.SONIOX_ENABLED}")
    logger.warning(f"HTTP  port: {Config.PORT}  (projector, personal, index)")
    logger.warning(f"HTTPS port: {Config.PORT_HTTPS} (audio streamer - microphone)")
    logger.warning(f"Log file: translation_server.log (rotating, 5 MB × 3)")
    logger.warning("=" * 70)

    # -----------------------------------------------------------------------
    # Graceful shutdown helper
    # -----------------------------------------------------------------------
    def _shutdown(reason: str = "shutdown"):
        logger.warning(f"Server shutting down ({reason})...")
        for sid in list(session_manager.sessions.keys()):
            try:
                session_manager.stop_session(sid)
            except Exception as exc:
                logger.error(f"Error stopping session '{sid}' during {reason}: {exc}")
        logger.warning("Server stopped")

    # Handle SIGTERM (systemctl stop / kill) gracefully
    def _handle_sigterm(*_):
        _shutdown("SIGTERM")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    # -----------------------------------------------------------------------
    # Background session age monitor — prevents zombie sessions all week
    # -----------------------------------------------------------------------
    def _session_age_monitor():
        while True:
            time.sleep(300)  # check every 5 minutes
            try:
                session_manager.purge_expired_sessions()
            except Exception as exc:
                logger.error(f"Session age monitor error: {exc}")

    _monitor_thread = threading.Thread(target=_session_age_monitor, daemon=True, name="session-age-monitor")
    _monitor_thread.start()

    # -----------------------------------------------------------------------
    # SSL certificate for HTTPS streamer port
    # -----------------------------------------------------------------------
    ssl_cert, ssl_key = ensure_ssl_cert()
    if not ssl_cert:
        logger.warning("pyOpenSSL unavailable — HTTPS port will not start. Streamer may not work on remote devices.")

    # -----------------------------------------------------------------------
    # Auto-start main session
    # -----------------------------------------------------------------------
    if Config.TESTING_MODE or Config.SONIOX_ENABLED:
        start_main_session(duration_minutes=90)
    else:
        logger.warning("Soniox API key not set. Set SONIOX_API_KEY or enable TESTING_MODE=true")

    # -----------------------------------------------------------------------
    # Dual-port WebSocket-aware server
    # -----------------------------------------------------------------------
    # We use socketio.run() (not werkzeug make_server) because only socketio.run()
    # correctly handles the HTTP→WebSocket Upgrade handshake in threading mode.
    # plain make_server is a WSGI-only server and silently rejects WebSocket upgrades.
    #
    # Strategy:
    #   • HTTP  (port 5000) — run in a daemon thread via socketio.run()
    #   • HTTPS (port 5443) — run on the main thread via socketio.run() with ssl_context
    # Both calls share the same `app` and `socketio` objects, so all rooms/sessions
    # are visible to clients on either port.
    # -----------------------------------------------------------------------

    try:
        import ssl as _ssl

        # --- HTTP listener (port 5000) ---
        def _run_http():
            logger.warning(f"HTTP server listening on http://0.0.0.0:{Config.PORT}")
            socketio.run(
                app,
                host=Config.HOST,
                port=Config.PORT,
                debug=False,
                use_reloader=False,
                log_output=Config.DEBUG,
                allow_unsafe_werkzeug=True,   # allow production use of built-in server
            )

        http_thread = threading.Thread(target=_run_http, daemon=True, name="http-server")
        http_thread.start()

        # --- HTTPS listener (port 5443) ---
        if ssl_cert and ssl_key:
            ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(certfile=ssl_cert, keyfile=ssl_key)

            logger.warning(f"HTTPS server listening on https://0.0.0.0:{Config.PORT_HTTPS}")
            logger.warning("NOTE: Remote devices must accept the self-signed cert once in their browser.")

            # Block main thread here; HTTP runs in daemon thread above.
            socketio.run(
                app,
                host=Config.HOST,
                port=Config.PORT_HTTPS,
                debug=False,
                use_reloader=False,
                log_output=Config.DEBUG,
                ssl_context=ssl_ctx,
                allow_unsafe_werkzeug=True,
            )
        else:
            # No SSL — just keep the HTTP thread alive
            http_thread.join()

    except KeyboardInterrupt:
        _shutdown("KeyboardInterrupt")
