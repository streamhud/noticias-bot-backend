import time
import os
import re
import html
import json
import feedparser
import requests
from gtts import gTTS
from dotenv import load_dotenv
import google.generativeai as genai
from supabase import create_client, Client

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

genai.configure(api_key=GEMINI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

FEEDS = [
    "https://radio3cadenapatagonia.com.ar/feed/",
    "https://www.elchubut.com.ar/rss",
    "https://www.diariojornada.com.ar/rss/",
    "https://www.adnsur.com.ar/rss/feed.xml",
    "https://www.red43.com.ar/rss",
    "https://purapasion.com.ar/feed/",
    "https://news.google.com/rss?hl=es-419&gl=AR&ceid=AR:es-419",
    "https://www.infobae.com/feed/",
    "https://tn.com.ar/feed/",
    "https://news.google.com/rss/search?q=catástrofe+OR+sismo+OR+atentado+OR+falleció+OR+muerte+OR+alerta+urgente&hl=es-419&gl=AR&ceid=AR:es-419"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def limpiar_texto(texto):
    if not texto:
        return ""
    texto_decodificado = html.unescape(texto)
    texto_sin_html = re.sub(r'<[^>]+>', '', texto_decodificado)
    return ' '.join(texto_sin_html.split()).strip()

def obtener_metadatos_html(url):
    try:
        respuesta = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        url_real = respuesta.url
        html_contenido = respuesta.text
        
        img_match = re.search(r'<meta\s+(?:property|name)="og:image"\s+content="([^"]+)"', html_contenido, re.IGNORECASE)
        imagen_url = img_match.group(1) if img_match else None
        
        if imagen_url and any(dominio in imagen_url for dominio in ["googleusercontent.com", "gstatic.com", "google.com"]):
            imagen_url = None

        yt_match = re.search(r'(?:youtube\.com\/(?:embed\/|watch\?v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})', html_contenido)
        media_embed = None
        if yt_match:
            video_id = yt_match.group(1)
            media_embed = f'<iframe class="w-full aspect-video rounded-xl shadow-lg mb-4" src="https://www.youtube.com/embed/{video_id}" frameborder="0" allowfullscreen></iframe>'
            
        return imagen_url, media_embed, url_real
    except Exception as e:
        print(f"Error extrayendo HTML de {url}: {e}")
        return None, None, url

def extraer_imagen_rss(entry):
    url_img = None
    if 'media_content' in entry and len(entry.media_content) > 0:
        url_img = entry.media_content[0]['url']
    elif 'enclosures' in entry:
        for enc in entry.enclosures:
            if 'image' in enc.get('type', ''):
                url_img = enc.get('href')
                break
    if not url_img:
        img_match = re.search(r'<img[^>]+src="([^">]+)"', entry.get('description', ''))
        if img_match:
            url_img = img_match.group(1)
            
    if url_img and any(dominio in url_img for dominio in ["googleusercontent.com", "gstatic.com", "google.com"]):
        return None
        
    return url_img

def generar_audio_mp3(texto_audio, filename="temp_audio.mp3"):
    texto_limpio_audio = limpiar_texto(texto_audio)
    if not texto_limpio_audio:
        return None

    try:
        # Voz en español con acento argentino vía gTTS
        tts = gTTS(text=texto_limpio_audio, lang='es', tld='com.ar')
        tts.save(filename)
        return filename
    except Exception as e:
        print(f"❌ Error generando TTS con gTTS: {e}")
        return None

def subir_audio_a_supabase(file_path, destination_name):
    try:
        with open(file_path, 'rb') as f:
            supabase.storage.from_('audios').upload(
                path=destination_name,
                file=f,
                file_options={"content-type": "audio/mpeg", "x-upsert": "true"}
            )
        return supabase.storage.from_('audios').get_public_url(destination_name)
    except Exception as e:
        print(f"Error subiendo audio: {e}")
        return None

def procesar_noticia_con_gemini(titulo, descripcion):
    titulo_limpio = limpiar_texto(titulo)
    resumen_limpio = limpiar_texto(descripcion)

    try:
        model = genai.GenerativeModel("gemini-3.8-flash")
        prompt = f"""
        Sos el editor principal de un portal de noticias automatizado.
        Procesá la noticia y devolvé ÚNICAMENTE un objeto JSON válido.
        
        Campos requeridos:
        - "titulo": Título claro y atractivo.
        - "categoria": Elegí STRICTAMENTE una: ["Deportes", "Policiales", "Cultura", "Política", "Sociedad", "Economía", "Nacionales", "Mundo", "Alertas"].
        - "puntos_clave": Viñetas en Markdown (*).
        - "texto_audio": Resumen ultra breve de máximo 2 oraciones para voz.

        Título original: {titulo_limpio}
        Texto: {resumen_limpio}
        """
        response = model.generate_content(prompt)
        texto_limpio_json = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(texto_limpio_json)
    except Exception as e:
        print(f"⚠️ Aviso Gemini API ({e}). Usando Fallback directo...")
        
        puntos_fallback = f"* {resumen_limpio[:220]}..." if len(resumen_limpio) > 220 else f"* {resumen_limpio}"
        audio_fallback = resumen_limpio[:180] if resumen_limpio else titulo_limpio

        return {
            "titulo": titulo_limpio,
            "categoria": "Informativa",
            "puntos_clave": puntos_fallback,
            "texto_audio": audio_fallback
        }

def ejecutar_bot():
    print("Iniciando escaneo masivo...")
    for feed_url in FEEDS:
        try:
            respuesta = requests.get(feed_url, headers=HEADERS, timeout=15)
            feed = feedparser.parse(respuesta.content)
            print(f"\nEscaneando feed: {feed_url} ({len(feed.entries)} noticias)")

            for entry in feed.entries[:3]:
                fuente_url = entry.link
                
                res = supabase.from_('noticias').select('id').eq('fuente_url', fuente_url).execute()
                if res.data and len(res.data) > 0:
                    print(f"⏭️ Ya existe: {entry.title[:30]}...")
                    continue

                titulo_orig = entry.title
                resumen_orig = entry.get('summary', entry.get('description', ''))
                
                data_ia = procesar_noticia_con_gemini(titulo_orig, resumen_orig)

                imagen_real, video_real, fuente_url_real = obtener_metadatos_html(fuente_url)
                if not imagen_real:
                    imagen_real = extraer_imagen_rss(entry)

                audio_url = None
                if data_ia.get("texto_audio"):
                    temp_file = f"audio_{abs(hash(fuente_url))}.mp3"
                    audio_generado = generar_audio_mp3(data_ia["texto_audio"], temp_file)
                    if audio_generado:
                        audio_url = subir_audio_a_supabase(temp_file, f"noticia_{abs(hash(fuente_url))}.mp3")
                        if os.path.exists(temp_file): 
                            os.remove(temp_file)

                nueva_noticia = {
                    "titulo": data_ia["titulo"],
                    "puntos_clave": data_ia["puntos_clave"],
                    "categoria": data_ia.get("categoria", "Informativa"),
                    "fuente_url": fuente_url_real,
                    "audio_url": audio_url,
                    "media_embed": video_real,
                    "imagen_url": imagen_real
                }

                supabase.from_('noticias').insert(nueva_noticia).execute()
                print(f"✅ Publicada [{data_ia.get('categoria')}]: {data_ia['titulo']}")
                time.sleep(2)

        except Exception as e:
            print(f"Error en feed {feed_url}: {e}")

if __name__ == "__main__":
    ejecutar_bot()
