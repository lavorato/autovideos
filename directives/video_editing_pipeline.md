# Diretiva: Pipeline de Edição de Vídeo

## Objetivo
Processar vídeos brutos de gravação, aplicando uma sequência de edições automatizadas para produzir vídeos finalizados com qualidade profissional.

## Entradas
- Vídeos brutos em `input/` (formatos: .mp4, .mov, .mkv, .avi, .webm)

## Saídas
- Vídeos finalizados em `output/`
- Arquivos intermediários em `.tmp/` (podem ser apagados após conclusão)

## Pipeline de Edição (ordem de execução)

### Passo 1: Transcrição e Análise
- **Script**: `execution/01_transcribe.py`
- Transcreve o áudio usando Whisper
- Gera arquivo de transcrição com timestamps em `.tmp/{video}_transcript.json`
- Identifica filler words e retakes no texto

### Passo 2: Remover Retakes
- **Script**: `execution/02_remove_retakes.py`
- Analisa a transcrição para detectar frases repetidas/retakes
- Retakes são identificados quando o falante repete a mesma frase ou começa de novo
- Mantém apenas a última versão de cada trecho
- Gera novo vídeo sem os retakes em `.tmp/{video}_no_retakes.mp4`

### Passo 3: Remover Filler Words
- **Script**: `execution/03_remove_fillers.py`
- Remove silêncios longos e filler words (éh, ah, uhm, tipo, né, então, assim, hm, ahn)
- Preserva pausas naturais curtas (~0.3s) para manter naturalidade
- Gera `.tmp/{video}_no_fillers.mp4`

### Passo 4: Melhorar Áudio (Studio Sound)
- **Script**: `execution/04_studio_sound.py`
- Redução de ruído de fundo (noise gate + spectral subtraction)
- Compressão dinâmica para nivelar volume
- EQ para voz (boost 2-5kHz para clareza, cut <80Hz para remover rumble)
- Normalização de loudness (target -16 LUFS)
- Gera `.tmp/{video}_studio.mp4`

### Passo 5: Corrigir Silêncio/Mute no Meio do Vídeo
- **Script**: `execution/05_fix_mute.py`
- Detecta trechos com áudio mudo inesperado no meio do vídeo
- Tenta interpolar/crossfade o áudio ao redor do gap
- Se não for possível, aplica ambient noise suave para evitar corte brusco
- Gera `.tmp/{video}_fixed_audio.mp4`

### Passo 6: Dividir em Cenas
- **Script**: `execution/06_split_scenes.py`
- Usa PySceneDetect para identificar mudanças de cena
- Gera marcadores de cena em `.tmp/{video}_scenes.json`
- Opcionalmente divide em arquivos separados em `.tmp/scenes/`

### Passo 7: Correção de Cores
- **Script**: `execution/07_color_correction.py`
- Auto white balance
- Correção de exposição
- Leve aumento de saturação e contraste
- Aplicação de LUT cinematográfico sutil
- Gera `.tmp/{video}_color.mp4`

### Passo 8: Efeitos de Zoom e PAN (Engajamento)
- **Script**: `execution/08_zoom_pan.py`
- Usa MediaPipe para detectar face do sujeito
- A cada ~6 segundos, aplica efeito de zoom suave (1.0x → 1.15x) ou PAN
- Centraliza no rosto do sujeito
- Transições suaves com easing
- Gera `.tmp/{video}_effects.mp4`

### Passo 8b: Hard Cut Zoom (Dinâmico)
- **Script**: `execution/08b_hard_cut_zoom.py`
- A cada ~3 segundos, alterna instantaneamente entre plano aberto (1.0x) e close-up (1.4x) centralizado no rosto
- Sem transição/easing — corte seco (hard cut) para criar ritmo dinâmico estilo TikTok/YouTube
- Usa MediaPipe para detecção facial
- Se não detectar face, usa centro do frame
- Processamento via OpenCV frame a frame + mux de áudio com FFmpeg
- Gera `.tmp/{video}_hardcut.mp4`

### Passo 8c: B-Roll Overlay
- **Script**: `execution/08c_broll.py`
- Procura pasta `input/{video_base}/` com assets (vídeos, fotos)
- Se a pasta não existir, passo é pulado automaticamente
- Analisa o transcript para encontrar o melhor momento para cada asset via keywords do nome do arquivo
- Se não encontrar match por keyword, distribui uniformemente ao longo do vídeo
- Split-view animado: o B-roll aparece ao lado do vídeo principal (slide-in com fade)
- Mínimo 3 segundos por B-roll, máximo 6 segundos
- Posições cíclicas: left, right, top, bottom com animações variadas
- Renderização: tenta Remotion (ProRes 4444 com alpha) primeiro, cai para FFmpeg se falhar
- Projeto Remotion em `execution/broll-renderer/` (requer `npm install`)
- Gera `.tmp/{video}_broll.mp4`

### Passo 9: Legendas com captacity
- **Script**: `execution/09_captions.py`
- Usa biblioteca captacity (https://github.com/unconv/captacity) para renderizar legendas
- Reutiliza transcrição Whisper do Passo 1 via parâmetro `segments` (evita re-transcrição)
- Estilo Clean Paragraph: Plus Jakarta Sans, branco com outline preto, highlight da palavra atual em azul (#1856FF)
- Sombra suave para legibilidade
- Posicionamento: centro inferior
- Gera arquivo final em `output/{video}_final.mp4`

## Edge Cases
- Se o vídeo não tem áudio, pular passos de áudio (2, 3, 4, 5)
- Se não detectar face, zoom/pan usa centro do frame
- Se a transcrição estiver vazia, pular remoção de fillers e retakes
- Se não existir pasta `input/{base}/` com assets, passo 8c é pulado automaticamente
- Se Remotion falhar (bundling, versão, etc.), B-roll usa FFmpeg como fallback
- Vídeos muito curtos (<30s): reduzir frequência de zoom/pan para cada 3s

## Dependências
- FFmpeg 8.0+
- Python 3.11+
- whisper, moviepy, scenedetect, opencv-python, mediapipe
- numpy, scipy (para processamento de áudio)

## Aprendizados
- **FFmpeg sem libass/libfreetype**: O FFmpeg instalado via homebrew (8.0.1) não inclui `--enable-libass` nem `--enable-libfreetype`. Filtros `ass`, `subtitles` e `drawtext` não estão disponíveis. Solução: Passo 9 usa Pillow + MoviePy para renderizar legendas frame a frame.
- **MoviePy 2.x**: A versão instalada é 2.1.1. Import correto é `from moviepy import VideoFileClip` (não `moviepy.editor`). Método `fl()` foi renomeado para `transform()`. `with_opacity()` aceita apenas float, não callable — usar `vfx.FadeIn`/`vfx.FadeOut` para fade.
- **Vídeo vertical 2160x3840**: O primeiro vídeo testado (IMG_1503.MOV) é vertical (portrait), 411MB, 1m37s. Captions devem se adaptar à largura do vídeo.
- **MediaPipe broken**: MediaPipe FaceDetection falha com erro de protobuf (`RuntimeError: Failed to parse`). Incompatibilidade de versão protobuf. Solução: usar OpenCV Haar cascade (`haarcascade_frontalface_default.xml`) para detecção facial. Funciona bem para vídeos de talking head.
