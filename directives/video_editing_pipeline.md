# Diretiva: Pipeline de Edição de Vídeo

## Objetivo
Processar vídeos brutos de gravação, aplicando uma sequência de edições automatizadas para produzir vídeos finalizados com qualidade profissional.

## Entradas
- Vídeos brutos em `input/` (formatos: .mp4, .mov, .mkv, .avi, .webm)

## Saídas
- Vídeos finalizados em `output/`
- Arquivos intermediários em `.tmp/` (podem ser apagados após conclusão)

## Pipeline de Edição (ordem de execução)

### Passo 0: Conversão da Fonte (MOV → MP4)
- **Script**: `execution/00_convert_source.py`
- Transcodifica fontes pesadas (`.mov`, `.mkv`, `.avi`) para um `.mp4` H.264 yuv420p + AAC em qualidade visualmente lossless (libx264 veryfast CRF 20 + AAC 256k)
- Objetivo: rodar o restante do pipeline em cima de um arquivo menor e **muito mais rápido de decodificar** (H.264 8-bit decodifica 2–3× mais rápido que HEVC 10-bit do iPhone)
- Fontes já compactas (`.mp4`, `.webm`) passam direto, sem re-encode
- Saída: `.tmp/{base}.mp4` (mesmo basename do original → todos os intermediários `.tmp/{base}_*` continuam válidos)
- Cache: reutiliza a saída se já existir e não estiver vazia
- Após rodar com sucesso, o `run_pipeline` troca o `video_path` ativo para o arquivo transcodificado, de forma que os passos seguintes operam no arquivo menor automaticamente
- Overrides: `SOURCE_TRANSCODE_CRF` (default 20), `SOURCE_TRANSCODE_PRESET` (default `veryfast`)

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

### Passo 3b: Isolar Voz (Remoção de Ruído ML)
- **Script**: `execution/03b_isolate_voice.py`
- Usa **Demucs** (source separation da Meta, `htdemucs` por padrão) em modo `--two-stems=vocals` para separar a voz do falante de todo o resto (ar condicionado, trânsito, teclado, música, reverb de sala)
- Qualidade muito superior ao `afftdn` (filtro FFT) usado no passo 4 — ideal para gravações com ruído de fundo consistente
- Placement: roda **depois** do passo 3 (corte de fillers/retakes), portanto processa só o áudio já enxuto
- Saída: `.tmp/{video}_voice.mp4` (stream de vídeo copiado, áudio substituído pelo stem de vocals em ALAC)
- Device auto-detect: MPS (Apple Silicon) → CUDA → CPU
- Overrides: `VOICE_ISOLATION_MODEL` (default `htdemucs`; `mdx_extra` também bom), `VOICE_ISOLATION_DEVICE`, `VOICE_ISOLATION_SHIFTS` (default 1; 2-5 = maior qualidade, mais lento), `VOICE_ISOLATION_DISABLE=1` para pular
- Se o vídeo não tiver áudio, o passo copia o input como saída e segue em frente

### Passo 4: Melhorar Áudio (Studio Sound)
- **Script**: `execution/04_studio_sound.py`
- Input preferencial: `.tmp/{video}_voice.mp4` (saída do 3b); fallback para `_no_fillers.mp4` → vídeo original
- Redução de ruído de fundo (noise gate + spectral subtraction) — com voz já isolada pelo 3b, funciona como polimento residual
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
- **demucs** (≥ 4.0) + torch (para passo 3b de isolamento de voz)

## Aprendizados
- **Passo 0 / HEVC 10-bit do iPhone**: O MOV do iPhone é HEVC 10-bit yuv420p10le 4K (~25 Mbps). Testamos `h264_videotoolbox` q=75: codifica em 32s mas gera arquivo **maior** (461MB vs 348MB), porque HEVC 10-bit é muito mais eficiente que H.264 8-bit a qualidade equivalente. Solução: usar `libx264 veryfast CRF 20` — leva ~51s no 4K de 116s, gera saída 22% menor (271MB) em H.264 yuv420p 8-bit, que decodifica 2–3× mais rápido nos passos seguintes. Áudio: AAC 256k (transparente para voz).
- **FFmpeg sem libass/libfreetype**: O FFmpeg instalado via homebrew (8.0.1) não inclui `--enable-libass` nem `--enable-libfreetype`. Filtros `ass`, `subtitles` e `drawtext` não estão disponíveis. Solução: Passo 9 usa Pillow + MoviePy para renderizar legendas frame a frame.
- **MoviePy 2.x**: A versão instalada é 2.1.1. Import correto é `from moviepy import VideoFileClip` (não `moviepy.editor`). Método `fl()` foi renomeado para `transform()`. `with_opacity()` aceita apenas float, não callable — usar `vfx.FadeIn`/`vfx.FadeOut` para fade.
- **Vídeo vertical 2160x3840**: O primeiro vídeo testado (IMG_1503.MOV) é vertical (portrait), 411MB, 1m37s. Captions devem se adaptar à largura do vídeo.
- **MediaPipe broken**: MediaPipe FaceDetection falha com erro de protobuf (`RuntimeError: Failed to parse`). Incompatibilidade de versão protobuf. Solução: usar OpenCV Haar cascade (`haarcascade_frontalface_default.xml`) para detecção facial. Funciona bem para vídeos de talking head.
