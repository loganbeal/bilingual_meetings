"""
Simple test script to verify Soniox Real-Time API connection and translation.
This script tests basic connectivity without the full Flask/SocketIO infrastructure.
"""

import os
import json
import time
import pyaudio
from dotenv import load_dotenv

load_dotenv()

SONIOX_API_KEY = os.getenv('SONIOX_API_KEY', '')

if not SONIOX_API_KEY:
    print("ERROR: SONIOX_API_KEY not set in .env file")
    exit(1)

print("=" * 70)
print("Soniox Simple Connection Test")
print("=" * 70)
print(f"API Key: {SONIOX_API_KEY[:10]}..." if SONIOX_API_KEY else "Not set")
print()

try:
    from websockets.sync.client import connect
    print("✓ websockets library imported successfully")
except ImportError as e:
    print(f"✗ Missing websockets library: {e}")
    print("  Install with: pip install websockets")
    exit(1)

# Configuration for Soniox
config = {
    "api_key": SONIOX_API_KEY,
    "model": "stt-rt-v3",
    
    # Enable language identification and hints
    "language_hints": ["en", "es"],
    "enable_language_identification": True,
    
    # Two-way translation between English and Spanish
    "translation": {
        "type": "two_way",
        "language_a": "en",
        "language_b": "es",
    },
    
    # Audio format - raw PCM 16-bit
    "audio_format": "pcm_s16le",
    "sample_rate": 16000,
    "num_channels": 1,
    
    # Enable endpoint detection for natural pauses
    "enable_endpoint_detection": True,
}

print("Connecting to Soniox WebSocket...")
SONIOX_WEBSOCKET_URL = "wss://stt-rt.soniox.com/transcribe-websocket"

try:
    with connect(SONIOX_WEBSOCKET_URL) as ws:
        print("✓ Connected to Soniox WebSocket")
        
        # Send configuration
        print("Sending configuration...")
        ws.send(json.dumps(config))
        print("✓ Configuration sent")

        # Don't wait for a server response here — start streaming audio immediately.
        # Waiting can trigger server-side timeouts (408). Any responses will be
        # handled in the receive loop below.
        
        # Setup audio capture
        print("\nInitializing microphone...")
        audio = pyaudio.PyAudio()
        
        try:
            stream = audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=16000,
                input=True,
                frames_per_buffer=1024
            )
            print("✓ Microphone opened")
            
            print("\n" + "=" * 70)
            print("Recording for 10 seconds... SPEAK NOW!")
            print("Try saying something in English or Spanish")
            print("=" * 70)
            
            start_time = time.time()
            print('start_time:', start_time)
            audio_sent = 0
            
            # Send audio for 10 seconds
            while time.time() - start_time < 5:
                print('looping: time elapsed:', time.time() - start_time)
                # Read and send audio
                audio_chunk = stream.read(1024, exception_on_overflow=False)
                print('sending audio chunk:', len(audio_chunk))
                try:
                    ws.send(audio_chunk)
                    audio_sent += len(audio_chunk)
                    print('sent audio chunk bytes:', len(audio_chunk))
                except Exception as e:
                    print(f"✗ Error sending audio chunk: {e}")
                    # If the server closed the connection, break out and stop
                    break
                
                # Try to receive responses (non-blocking)
                try:
                    print('waiting for response...')
                    # Try a short timeout recv to avoid blocking audio streaming
                    response_str = ws.recv(timeout=0.1)
                    print(response_str)
                    response = json.loads(response_str)
                    
                    # Check for errors
                    if response.get('error_code') is not None:
                        print(f"\n✗ ERROR from Soniox: {response.get('error_message')}")
                        break
                    
                    # Process tokens
                    tokens = response.get('tokens', [])
                    if tokens:
                        for token in tokens:
                            text = token.get('text', '')
                            is_final = token.get('is_final', False)
                            language = token.get('language', '')
                            translation_status = token.get('translation_status', 'none')
                            
                            if text and text not in ['<end>', '<fin>']:
                                marker = "[FINAL]" if is_final else "[PARTIAL]"
                                if translation_status == 'translation':
                                    src_lang = token.get('source_language', '?')
                                    print(f"  {marker} TRANSLATION ({src_lang}→{language}): {text}")
                                else:
                                    print(f"{marker} {language.upper()}: {text}")
                    
                    # Check if finished
                    if response.get('finished'):
                        print("\n✓ Session finished by server")
                        break
                        
                except TimeoutError:
                    # No response available, continue sending audio
                    print('no response yet')
                    pass
                except Exception as e:
                    print(f"\nWarning receiving response: {e}")
                    # If the server closed the connection, stop sending
                    break
            
            print(f"\n✓ Sent {audio_sent:,} bytes of audio")
            
            # Send end-of-audio signal
            print("\nSending end-of-audio signal...")
            try:
                ws.send("")
            except Exception as e:
                print('Error sending end-of-audio signal:', e)
            
            # Read remaining responses
            print("Reading final responses...")
            try:
                #ws.settimeout(5)  # Wait up to 5 seconds for final responses
                while True:
                    response_str = ws.recv()
                    response = json.loads(response_str)
                    
                    tokens = response.get('tokens', [])
                    if tokens:
                        for token in tokens:
                            text = token.get('text', '')
                            is_final = token.get('is_final', False)
                            language = token.get('language', '')
                            translation_status = token.get('translation_status', 'none')
                            
                            if text and text not in ['<end>', '<fin>']:
                                marker = "[FINAL]" if is_final else "[PARTIAL]"
                                if translation_status == 'translation':
                                    src_lang = token.get('source_language', '?')
                                    print(f"  {marker} TRANSLATION ({src_lang}→{language}): {text}")
                                else:
                                    print(f"{marker} {language.upper()}: {text}")
                    
                    if response.get('finished'):
                        print("\n✓ Session finished")
                        break
                        
            except TimeoutError:
                print("✓ No more responses (timeout)")
            
            stream.stop_stream()
            stream.close()
            
        finally:
            audio.terminate()
            
        print("\n" + "=" * 70)
        print("Test completed successfully!")
        print("=" * 70)
        
except Exception as e:
    print(f"\n✗ Error: {e}")
    import traceback
    traceback.print_exc()

print("\nIf you saw translations above, Soniox is working correctly!")
print("If not, check:")
print("  1. API key is valid")
print("  2. Microphone permissions")
print("  3. You spoke during the recording")
