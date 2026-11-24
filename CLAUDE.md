# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Open-LLM-VTuber is a voice-interactive AI companion with Live2D avatar support that runs completely offline. It's a cross-platform Python application supporting real-time voice conversations, visual perception, and Live2D character animations. The project features modular architecture for LLM, ASR (Automatic Speech Recognition), TTS (Text-to-Speech), and other components.

## Essential Commands

### Development Setup
- **Install dependencies**: `uv sync` (uses uv package manager)
- **Run server**: `uv run run_server.py`
- **Run with verbose logging**: `uv run run_server.py --verbose`
- **Update project**: `uv run upgrade.py`

### Code Quality
- **Lint code**: `ruff check .`
- **Format code**: `ruff format .`
- **Run pre-commit hooks**: `pre-commit run --all-files`

### Server Configuration
- **Main config file**: `conf.yaml` (user configuration)
- **Default configs**: `config_templates/conf.default.yaml` and `config_templates/conf.ZH.default.yaml`
- **Character configs**: `characters/` directory (YAML files)

## Architecture Overview

### Core Components

**WebSocket Server** (`src/open_llm_vtuber/server.py`):
- FastAPI-based server handling WebSocket connections
- Serves frontend, Live2D models, and static assets
- Supports both main client and proxy WebSocket endpoints

**Service Context** (`src/open_llm_vtuber/service_context.py`):
- Central dependency injection container
- Manages all engines (LLM, ASR, TTS, VAD, etc.)
- Each WebSocket connection gets its own service context instance

**WebSocket Handler** (`src/open_llm_vtuber/websocket_handler.py`):
- Routes WebSocket messages to appropriate handlers
- Manages client connections, groups, and conversation state
- Handles audio data, conversation triggers, and Live2D interactions

### Modular Engine System

The project uses a factory pattern for all AI engines:

**Agent System** (`src/open_llm_vtuber/agent/`):
- `agent_factory.py` - Factory for creating different agent types
- `agents/` - Various agent implementations (basic_memory, hume_ai, letta, mem0)
- `stateless_llm/` - Stateless LLM implementations (Claude, OpenAI, Ollama, etc.)

**ASR Engines** (`src/open_llm_vtuber/asr/`):
- Support for multiple ASR backends: Sherpa-ONNX, FunASR, Faster-Whisper, OpenAI Whisper, etc.
- Factory pattern for engine selection based on configuration

**TTS Engines** (`src/open_llm_vtuber/tts/`):
- Multiple TTS options: Azure TTS, Edge TTS, MeloTTS, CosyVoice, GPT-SoVITS, etc.
- Configurable voice cloning and multi-language support

**VAD (Voice Activity Detection)** (`src/open_llm_vtuber/vad/`):
- Silero VAD for detecting speech activity
- Essential for voice interruption without feedback loops

### Configuration Management

**Config System** (`src/open_llm_vtuber/config_manager/`):
- Type-safe configuration classes for each component
- Automatic validation and loading from YAML files
- Support for multiple character configurations and config switching

### Conversation System

**Conversation Handling** (`src/open_llm_vtuber/conversations/`):
- `conversation_handler.py` - Main conversation orchestration
- `single_conversation.py` - Individual user conversations
- `group_conversation.py` - Multi-user group conversations
- `tts_manager.py` - Audio streaming and TTS management

### MCP (Model Context Protocol) Integration

**MCP System** (`src/open_llm_vtuber/mcpp/`):
- Tool execution and server registry
- JSON detection and parameter extraction
- Integration with various MCP servers for extended functionality

## Key Development Patterns

### Error Handling
The codebase uses the missing `_cleanup_failed_connection` method pattern - when implementing new WebSocket handlers, ensure proper cleanup methods are implemented.

### Live2D Integration
- Models stored in `live2d-models/` directory
- Each model has its own `.model3.json` configuration
- Expression and motion control through WebSocket messages

### Audio Processing
- Real-time audio streaming through WebSocket
- Voice interruption support without headphones
- Multi-format audio support with proper codec handling

### Multi-language Support
- Character configurations support multiple languages
- TTS translation capabilities (speak in different language than input)
- I18n system for UI elements

## Important File Locations

- **Entry point**: `run_server.py`
- **Main server**: `src/open_llm_vtuber/server.py`
- **WebSocket routing**: `src/open_llm_vtuber/routes.py`
- **Configuration**: `conf.yaml` (user), `config_templates/` (defaults)
- **Frontend**: `frontend/` (Git submodule)
- **Live2D models**: `live2d-models/`
- **Character definitions**: `characters/`
- **Chat history**: `chat_history/`
- **Cache**: `cache/` (audio files, temporary data)

## Development Guidelines

### Adding New Engines
1. Create interface in appropriate directory (e.g., `asr_interface.py`)
2. Implement concrete class following existing patterns
3. Add to factory class (e.g., `asr_factory.py`)
4. Update configuration classes in `config_manager/`
5. Add configuration options to default YAML files

### WebSocket Message Handling
1. Add message type to `MessageType` enum in `websocket_handler.py`
2. Create handler method following `_handle_*` pattern
3. Register in `_init_message_handlers()` dictionary
4. Ensure proper error handling and client response

### Configuration Changes
- Always update both default config templates
- Maintain backward compatibility when possible
- Use the upgrade system for breaking changes
- Validate configurations in respective config manager classes

## Testing and Quality Assurance

The project uses:
- **Ruff** for linting and formatting (configured in `pyproject.toml`)
- **Pre-commit hooks** for automated quality checks
- **GitHub Actions** for CI/CD (`.github/workflows/`)
- Manual testing through web interface and desktop client

## Package Management

Uses **uv** (modern Python package manager):
- Dependencies defined in `pyproject.toml`
- Lock file: `uv.lock`
- Generated requirements: `requirements.txt` (auto-generated)
- Optional dependencies for specific features (e.g., `bilibili` extra)
## [deskpet] The pet's brain (since 2026-11-24)

- **Chat model**: Claude over the Anthropic API, through `src/open_llm_vtuber/deskpet/pet_llm.py` (`PetClaude`, provider `claude_llm` in `conf.yaml`). One request both decides what the user's sentence is for and answers it: the model has tools (`deskpet/brain.py`: `do_on_computer`, `stop_task`, `look_into`, `look_at_screen`, `approve`, `stop_listening`, `remember`, `forget`) and is told how things stand each turn (`brain.state_note`). What steers it is `brain.GUIDE` plus the persona in `conf.yaml`, not keyword rules: change behaviour there first.
- **`deskpet/router.py`**: with the cloud usable, `route()` only notes the turn and returns `None`. The old keyword rules and small-model classifier live on in `route_local()`, the backup path used while the API can't be reached; the chat then falls back to the local Ollama model (`fallback_model` in `conf.yaml`), which is loaded on demand for five minutes and otherwise stays out of the graphics memory.
- **`deskpet/cloud.py`**: the API key comes from the user environment variable `DeskPet` (never write it to a file or a log); small one-word questions go to `claude-haiku-5-5` (`cloud.small`); monthly budget and tally in `agent/cloud_usage.json`.
- **Regression tests that leave the running pet alone**: `../backups/claude_api_20261124/tests/` (`brain_flow_test.py` for whole turns with the executor stubbed, `intent_replay.py` to replay `agent/intent_log.jsonl`, `live_ws_check.py` for a silent turn on the live server). Run them from this folder with `.venv/Scripts/python.exe`.
- **The local model keeps the old rules** (user, 2026-11-24): whenever the local model does the talking - as backup while the API is unreachable, or because `llm_provider` was set back to `ollama_llm` - it gets the old persona (`prompts/utils/deskpet_local_persona.txt`, swapped in by `brain.local_system`) and `route_local()`'s keyword rules decide for it. `persona_prompt` in `conf.yaml` and `brain.GUIDE` are for Claude only. Use `127.0.0.1`, not `localhost`, in Ollama addresses: on this PC every request to `localhost:11434` starts 2 s late.
- **Her voice**: GPT-SoVITS takes about 0.55 s per piece whatever its length, and does not serve waiting requests in order of arrival, so `tts/gpt_sovits_tts.py` asks for one sentence at a time, in the order spoken (`async_generate_audio`). The first piece of a reply ends at the earliest comma or finished "……" (`utils/sentence_divider.py`). Every turn logs where its time went: `[deskpet] turn: heard | text | said | voice (synthesis) | sent` (`deskpet/timing.py`); `stack_probe.py` in the tests folder shows the same on a spare port without touching the running pet.
