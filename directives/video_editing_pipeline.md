# Diretiva: Pipeline de Edição de Vídeo

## Objetivo
Processar vídeos brutos de gravação, aplicando uma sequência de edições automatizadas para produzir vídeos finalizados com qualidade profissional.

## Entradas
- Vídeos brutos em `input/` (formatos: .mp4, .mov, .mkv, .avi, .webm)

## Saídas
- Vídeos finalizados em `output/`
- Arquivos intermediários em `.tmp/` (podem ser apagados após conclusão)

## Modo Always-On (pré-processamento automático)

Antes da edição manual, é útil deixar um "ingestor" em `input/`: ele roda **só o passo 00** (conversão → `.tmp/{base}.mp4`). **A transcrição (01) corre depois do corte** no `00b_editor`, para os timestamps baterem com o vídeo já cortado.

- **Script**: `execution/watch_input.py` (também acessível via `python execution/run_pipeline.py --watch`)
- Polling sem dependências externas (scan em `input/` a cada N segundos, default 3s)
- **Stable-file detection**: só processa quando `(size, mtime)` ficam idênticos em dois polls consecutivos — evita pegar arquivo no meio de um AirDrop/cópia do iPhone
- Cache-aware: reaproveita `.tmp/{base}.mp4` do passo 00 quando já existir
- Sequencial (um vídeo por vez) — sem competição por CPU/ `.tmp/` quando chegam múltiplos arquivos juntos
- Ignora: dotfiles (`.DS_Store`), subpastas (`input/IMG_1792/` é tratada pelo passo 08c), extensões que não são vídeo

Fluxo recomendado:
```bash
# terminal 1 — deixar ligado durante a sessão
python execution/watch_input.py
# (equivalente: python execution/run_pipeline.py --watch)

# terminal 2 — após "✓ ready to trim": cortar no editor e marcar trim feito
python execution/00b_editor.py            # trim primeiro; sidebar → "Mark trim done"

# (transcrição: ao marcar trim no 00b corre em segundo plano por defeito;
#  ou ``python execution/run_pipeline.py .tmp/IMG_1792.mp4 --only 01`` se EDITOR_AUTO_TRANSCRIBE=0)

# terminal 3 — após transcrever: **Save** (só grava) ou **Save and continue** no 00b (02+ inicia com debounce se ``EDITOR_AUTO_PIPELINE=1``), ou ``--skip 00,01`` manual
python execution/run_pipeline.py input/IMG_1792.MOV --skip 00,01   # só necessário com auto-pipeline desligado
```

Flags do watcher: `--interval <sec>`, `--input-dir <path>`, `--tmp-dir <path>`, `--once` (scan único, útil em CI), `--force` (ignora cache). `Ctrl+C` pede parada graciosa — termina o arquivo em andamento e sai.

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

### Passo 0b: Editor Unificado (Interativo — opcional)
- **Script**: `execution/00b_editor.py`
- UI web local única (glassmorphism) que funde as duas ferramentas interativas antigas (`00b_trim_video.py` + `01b_fix_transcript.py`) em um só app.
- Ao abrir, mostra uma **sidebar com a lista de arquivos editáveis em `.tmp/`** agrupados por basename: cada grupo lista o `.mp4` (modo “Video trim”) e o `*_transcript.json` (modo “Transcript”). Clicar em um item troca o painel da direita para o editor apropriado.
- Modo **Video trim**: player HTML5 + timeline com marcadores de corte, atalhos `Space` play/pause · `←/→` ±0.1s · `Shift+←/→` ±1s · `I`/`O` cut-in/cut-out · `[`/`]` trim start/end. Ao salvar, o FFmpeg renderiza os segmentos mantidos (`filter_complex trim+atrim+concat`) e sobrescreve `.tmp/{base}.mp4` atomicamente, criando `.tmp/{base}.mp4.bak` no primeiro save.
- Modo **Transcript**: find/replace ciente de pontuação sobre `words`/`segments[*].words`/`text`, com lista de palavras únicas na lateral e histórico das substituições. **Save** grava o JSON (`.bak` no primeiro save) sem avançar; **Save and continue** confirma a revisão e inicia 02+ (ou só escreve o gate se `EDITOR_AUTO_PIPELINE=0`, usando **Run 02+** depois).
- **Passo manual**: não entra sozinho no `run_pipeline`. Ordem: **trim** → **Mark trim done** → **passo 01** sobre `.tmp/{base}.mp4` → **transcript** → **Save and continue** ou **Mark transcript review done** → passos 02+.
  ```bash
  python execution/00b_editor.py                              # abre sidebar com .tmp/
  python execution/00b_editor.py .tmp/IMG_1792.mp4            # pré-abre em Video trim
  python execution/00b_editor.py .tmp/IMG_1792_transcript.json  # pré-abre em Transcript
  python execution/00b_editor.py --port 5060                  # trocar porta (default 5058)
  python execution/00b_editor.py --mark-trim-done .tmp/IMG_1792.mp4
  python execution/00b_editor.py --mark-done .tmp/IMG_1792.mp4   # após existir transcript
  ```
- **Gates** (ficheiro `.tmp/{base}_editor_review.json`): **trim confirmado** antes do passo 01; **revisão da transcrição** antes dos passos 02+. Após **Mark trim done**, o passo 01 corre **automaticamente** no `00b_editor` (`EDITOR_AUTO_TRANSCRIBE`, defeito ligado). **Save and continue** na transcrição (não o **Save** simples) dispara 02+ após debounce se `EDITOR_AUTO_PIPELINE` estiver ligado; com `EDITOR_AUTO_PIPELINE=0`, grava o marker e usa **Run 02+** ou `run_pipeline.py --skip 00,01`. **Mark transcript review done** continua a confirmar e a iniciar o pipeline. Novo trim apaga o marker. Bypass: `--skip-editor-gate` / `PIPELINE_SKIP_EDITOR_GATE=1`.
- Como os arquivos mantêm o mesmo basename `{base}`, todos os intermediários `.tmp/{base}_*` ficam consistentes e os passos seguintes (02 em diante) podem rodar normalmente.
- Edge case (trim): se marcar cortes que cobririam o vídeo inteiro, o save é recusado com erro claro; se o vídeo não tiver áudio, `atrim`/mapeamento de áudio são omitidos automaticamente.

### Passo 1: Transcrição e Análise
- **Script**: `execution/01_transcribe.py`
- **Colocação**: depois do **corte** no `00b_editor` e de **Mark trim done** (o Whisper deve correr sobre `.tmp/{base}.mp4` já com a duração final).
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
- Alterna instantaneamente entre plano aberto (1.0x) e close-up (1.4x) centralizado no rosto
- Sem transição/easing — corte seco (hard cut) para criar ritmo dinâmico estilo TikTok/YouTube
- **Modo AI (padrão quando OpenRouter está configurado)**: lê `.tmp/{video}_transcript.json` e envia os segmentos para o OpenRouter (`OPENROUTER_API_KEY` + `OPENROUTER_MODEL(S)`) pedindo para identificar os momentos mais impactantes do discurso (punchlines, fatos-chave, beats emocionais, CTAs). Esses momentos viram as janelas de close-up; o restante fica em plano aberto.
- **Modo fallback (sem API / sem momentos / sem transcript)**: alterna a cada `CUT_INTERVAL` segundos (default 5.0s), plano aberto → close-up → plano aberto...
- Usa OpenCV Haar cascade para detecção facial (MediaPipe tem problemas de protobuf)
- Se não detectar face, usa centro do frame
- Processamento via filter_complex do FFmpeg (trim + crop + scale + concat)
- Gera `.tmp/{video}_hardcut.mp4`
- **Sidecar para o passo 8d**: os momentos escolhidos pela IA (ou lista vazia) também são gravados em `.tmp/{video}_zoom_moments.json` (`{ "moments": [{start, end, reason}, ...] }`) para que o passo 8d toque FX nesses beats sem precisar chamar o LLM de novo
- Tunables: `CUT_INTERVAL`, `ZOOM_LEVEL`, `AI_ZOOM_MIN_DURATION`, `AI_ZOOM_MAX_DURATION`, `AI_ZOOM_MAX_MOMENTS`, `AI_ZOOM_MIN_GAP`

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

### Passo 8d: FX Sounds (SFX nos momentos impactantes)
- **Script**: `execution/08d_fx_sounds.py`
- Lê `.tmp/{video}_zoom_moments.json` (gerado pelo 8b) e, para cada momento impactante escolhido pela IA (punchline, fato-chave, beat emocional, CTA), toca um FX aleatório de `fxs/` começando exatamente no `start` do momento
- Escolha dos FX é embaralhada com proteção anti-repetição: o mesmo efeito nunca toca duas vezes seguidas, mesmo quando há mais momentos que arquivos em `fxs/`
- Input preferencial: `.tmp/{video}_broll.mp4` → `_hardcut.mp4` → intermediários anteriores. O vídeo é copiado em stream (sem re-encode), só a faixa de áudio é remuxada em ALAC com `amix`
- Quando não há momentos da IA (sem transcript, sem OpenRouter, ou array vazio), ou quando `fxs/` está vazio, ou quando `FX_DISABLE=1`, o passo faz passthrough (stream-copy) para manter `.tmp/{video}_fx.mp4` como saída canônica que o 9 e o 10 consomem
- Tunables via env: `FX_VOLUME` (default 0.6), `FX_MAX_DURATION` (default 2.5s — trunca FX longos pra não cobrir a voz), `FX_DIR` (default `fxs`), `FX_DISABLE=1` pula
- Gera `.tmp/{video}_fx.mp4`

### Passo 8e: Data Viz Overlay (Porcentagens e dados)
- **Script**: `execution/08e_data_viz.py`
- Detecta dados numéricos citados na transcrição e substitui o vídeo por uma visualização fullscreen animada (progress ring + contador + label) por ~2s, com o áudio continuando por baixo (cutaway). Escopo inicial: porcentagens em PT-BR; arquitetura extensível (lista `DETECTORS`) pra adicionar dinheiro/datas/comparações depois.
- **Detecção determinística** (sem LLM): scan sobre `words[]` do transcript procurando 4 padrões:
  - `"100%"` (token único com `%`)
  - `"50" "%"` (dois tokens consecutivos)
  - `"50" "por" "cento"` / `"vinte" "por" "cento"` (três tokens, inclui números por extenso PT-BR: `zero..dezenove`, `vinte, trinta, ..., noventa`, `cem`, `cento`, `meio/metade`→50)
  - `"50" "porcento"` / `"vinte" "porcento"` (forma glued, dois tokens)
- Filtros: valor clampado em 0-100, gap mínimo entre moments (`MIN_GAP_BETWEEN_MOMENTS=1.5s`)
- **OpenRouter enrichment (opcional)**: quando `OPENROUTER_API_KEY` + `OPENROUTER_MODEL(S)` estão setados, manda até 12 moments com contexto (segmento da frase + antes/depois) pedindo JSON com `{label, duration, emphasis}`:
  - `label`: caption PT-BR curta (3-6 palavras, vai ficar abaixo do ring)
  - `duration`: float entre 1.5s e 3.0s
  - `emphasis`: `growth` (verde #07CA6B), `drop` (vermelho #EA2143) ou `neutral` (azul #1856FF) — define a paleta do card
- **Render**: usa HeyGen **HyperFrames** (HTML/CSS/GSAP, não Remotion). Projeto em `execution/dataviz-renderer/` com `package.json`, `hyperframes.json` e `templates/percentage-ring.html.tpl`. Para cada moment, o Python materializa um projeto one-off em `.tmp/dataviz_NN/` substituindo tokens (`__WIDTH__`, `__HEIGHT__`, `__VALUE__`, `__LABEL__`, `__DURATION__`, `__COLOR_*__`) e roda `npx hyperframes render --quality standard --fps {fps_do_video}`. Renderização em paralelo via `ThreadPoolExecutor` (default 2 workers, cada um instancia um Chrome headless ~500MB)
- **Template**: SVG progress ring (stroke-dashoffset tween 0→value%), contador central animado (GSAP onUpdate tweenando um escalar e escrevendo o valor arredondado no DOM), label com letterspacing, backdrop com radial glow tintado pelo `emphasis`. Fonte Plus Jakarta Sans, tipografia/cores vindas do design system do projeto (CLAUDE.md). Sizing responsivo via `min(vw, vh)` — funciona tanto em 1080x1920 quanto 2160x3840 sem mudar o template
- **Composite**: FFmpeg overlay com `enable='between(t,start,end)':format=auto`, áudio copiado em stream (`-c:a copy`). O overlay cobre o frame inteiro nos moments ativos — cutaway limpo
- **Placement no pipeline**: depois do 08d (fx_sounds), antes do 09 (captions). Os passos 09 e 10 foram atualizados pra procurar `_dataviz.mp4` como primeira opção na resolução de input
- **Saídas**:
  - `.tmp/{base}_dataviz.mp4` (vídeo com overlays)
  - `.tmp/{base}_dataviz_moments.json` (sidecar: lista de moments com `value/start/end/label/duration/emphasis/model`)
- **Fallbacks graciosos**:
  - sem transcript → skip silencioso
  - sem `%` detectado → passthrough (copia o input como `_dataviz.mp4` para manter o nome canônico na cadeia)
  - sem `OPENROUTER_API_KEY` → usa defaults (label = snippet da fala, emphasis=neutral, duration=2.0s)
  - `npx hyperframes` não encontrado → loga e passa adiante
  - clip individual falha → é descartado, o resto é compositado normalmente
- **Tunables via env**:
  - `DATAVIZ_DISABLE=1` pula o passo inteiro
  - `DATAVIZ_RENDER_WORKERS=<n>` (default 2) — quantos Chrome headless simultâneos
  - `OPENROUTER_MODEL(S)` — reutiliza os mesmos do 08b/08c
- **Dependências extras e setup (uma vez só)**:
  - Node.js ≥ 22 (pro `hyperframes` 0.4+)
  - Skills instaladas com `npx skills add heygen-com/hyperframes` e `npx skills add remotion-dev/skills` (só a skill `hyperframes` é usada em runtime; a do Remotion serve pro `broll-renderer` do 08c)
  - **Install local do hyperframes** (obrigatório — não usar `npx --yes hyperframes`): `cd execution/dataviz-renderer && PUPPETEER_SKIP_DOWNLOAD=1 npm install`. Motivo: `npx` resolve o Node module search pra cima e pode pegar um `puppeteer` desatualizado no home do usuário (`/Users/.../node_modules/puppeteer`), cujo `createCDPSession` não existe. O install local "sombra" essa resolução com puppeteer 24.x moderno
  - **Chrome Headless Shell 131** instalado em `~/.cache/puppeteer/chrome-headless-shell/`: `npx --yes @puppeteer/browsers@latest install chrome-headless-shell@131.0.6778.85 --path ~/.cache/puppeteer`. Motivo: Hyperframes 0.4.12 fixou Chrome 131; Chrome do sistema mais novo (140+) quebra o CDP. O `.npmrc` em `dataviz-renderer/` já tem `puppeteer_skip_download=true` porque usamos o Chrome Headless Shell instalado acima, não o que vem com puppeteer

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
