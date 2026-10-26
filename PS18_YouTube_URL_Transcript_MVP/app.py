import os, re, json, logging, html
from datetime import datetime, date, time as dt_time
from urllib.parse import urlparse, parse_qs
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv
from agents import configure_logging

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER=logging.getLogger("ps18.app")
configure_logging()
st.set_page_config(page_title="PS-18 | Creator Studio", page_icon="🎬", layout="wide")

st.markdown("""
<style>
.block-container {max-width:1540px;padding-top:1.1rem;padding-bottom:3rem}
.hero {padding:1.2rem 1.5rem;border-radius:16px;background:linear-gradient(110deg,#17181c,#29242e);border:1px solid #35343a;color:white;margin-bottom:1rem}
.hero h1 {margin:0 0 .35rem 0;font-size:2rem;letter-spacing:-.04em}
.hero p {margin:0;color:#c7c5ce}
[data-testid="stSidebar"] {background:#101114;border-right:1px solid #282a2f}
[data-testid="stSidebar"] * {color:#eceef2}
.studio-chip {border:1px solid #3b3d43;background:#25272c;color:#e8e9ec;border-radius:999px;padding:6px 11px;font-size:12px;display:inline-block}
.metric-card {padding:14px 16px;border:1px solid #2d3036;background:#181a1f;border-radius:13px;min-height:82px}
.metric-label {font-size:11px;color:#a7abb4;text-transform:uppercase;letter-spacing:.07em}
.metric-value {font-size:23px;font-weight:700;color:#f7f7f8;margin-top:5px}
.meme-card {padding:20px;border-radius:16px;border:1px solid #47354e;background:radial-gradient(circle at 85% 0%,#573568 0,#29202f 46%,#191a20 100%);min-height:250px;color:#fff}
.meme-kicker {font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:#dfb3ff}
.meme-caption {font-size:26px;line-height:1.16;font-weight:800;margin:18px 0;color:white}
.meme-evidence {font-size:12px;color:#c3c2ca;border-top:1px solid #ffffff24;padding-top:12px}
div[data-testid="stTabs"] button {font-weight:650}
</style>
<div class="hero"><h1>🎬 PS-18 · Creator Studio</h1>
<p>Paste a YouTube URL → analyze its available transcript → discover timestamped meme moments.</p></div>
""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown("### 🎬 Studio controls")
    target_len = st.slider("Target moment length (seconds)", 3, 8, 6)
    max_candidates = st.slider("Maximum moments to keep", 3, 250, 100)
    st.caption("Every transcript chunk is scanned. This limit controls how many ranked results are retained.")
    st.divider()
    channel_id=os.getenv("YOUTUBE_CHANNEL_ID","").strip()
    st.caption("Creator channel · mock queue")
    st.code(channel_id or "YOUTUBE_CHANNEL_ID not configured",language=None)
    st.caption("Queue actions are simulated locally; nothing is published.")

def parse_video_id(url):
    u=urlparse(url.strip())
    host=u.netloc.lower().replace("www.","")
    if host in ("youtu.be",):
        return u.path.strip("/").split("/")[0]
    if host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        if u.path == "/watch":
            return parse_qs(u.query).get("v",[""])[0]
        m=re.match(r"/(?:embed|shorts|live)/([^/?]+)",u.path)
        if m: return m.group(1)
    return ""

def fmt_time(sec):
    sec=max(0,int(sec))
    h=sec//3600; m=(sec%3600)//60; s=sec%60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def fetch_transcript(video_id, language):
    from youtube_transcript_api import YouTubeTranscriptApi
    api=YouTubeTranscriptApi()
    # New API returns a transcript list object; try requested languages first, then available.
    transcript_list=api.list(video_id)
    try:
        transcript=transcript_list.find_transcript(language)
    except Exception:
        try:
            transcript=transcript_list.find_generated_transcript(language)
        except Exception:
            transcript=next(iter(transcript_list))
    entries=transcript.fetch()
    rows=[]
    for e in entries:
        # compatible with dict-like and snippet objects
        if isinstance(e,dict):
            start=e.get("start",0); duration=e.get("duration",0); text=e.get("text","")
        else:
            start=getattr(e,"start",0); duration=getattr(e,"duration",0); text=getattr(e,"text","")
        rows.append({"start":float(start),"end":float(start)+float(duration),"text":str(text).replace("\n"," ").strip()})
    return rows, getattr(transcript,"language_code",language), getattr(transcript,"is_generated",None)

def make_candidates(rows, language, max_candidates, target_len, progress_callback=None):
    from agents import analyze_comedy
    return analyze_comedy(rows, language, max_candidates, target_len, progress_callback)

def parse_transcript_file(upload):
    """Read a timestamped SRT/VTT transcript or plain text transcript upload."""
    raw=upload.getvalue().decode("utf-8-sig",errors="replace")
    rows=[]
    timestamp=re.compile(r"(?:(\d{1,2}:)?\d{1,2}:\d{2})[.,](\d{3})\s*-->\s*(?:(\d{1,2}:)?\d{1,2}:\d{2})[.,](\d{3})")
    def seconds(hms,ms):
        parts=[int(x) for x in hms.split(":")]
        if len(parts)==2: parts=[0]+parts
        return parts[0]*3600+parts[1]*60+parts[2]+int(ms)/1000
    lines=raw.replace("\r","").split("\n")
    i=0
    while i<len(lines):
        match=timestamp.search(lines[i])
        if match:
            start=seconds(match.group(1) or "0:00",match.group(2)); end=seconds(match.group(3) or "0:00",match.group(4))
            i+=1; text=[]
            while i<len(lines) and lines[i].strip() and not timestamp.search(lines[i]):
                line=lines[i].strip()
                if not line.isdigit() and line.upper()!="WEBVTT": text.append(re.sub(r"<[^>]+>","",line))
                i+=1
            if " ".join(text).strip(): rows.append({"start":start,"end":end,"text":" ".join(text).strip()})
        else: i+=1
    if rows: return rows
    text=" ".join(line.strip() for line in lines if line.strip() and not line.upper().startswith("WEBVTT"))
    if not text: return []
    # Plain text has no source timings; create approximate reading-time anchors and label them as estimates.
    sentences=re.split(r"(?<=[.!?])\s+",text)
    cursor=0.0
    for sentence in sentences:
        if sentence.strip():
            duration=max(1.0,len(sentence.split())/2.5)
            rows.append({"start":cursor,"end":cursor+duration,"text":sentence.strip()})
            cursor+=duration
    return rows

def embed_window(video_id,start,end):
    # YouTube's supported player start/end parameters seek to a segment in the original video.
    start=max(0,int(start)); end=max(start+1,int(end))
    src=f"https://www.youtube-nocookie.com/embed/{video_id}?start={start}&end={end}&autoplay=0&rel=0"
    components.html(f"""
    <div style="position:relative;width:100%;padding-bottom:56.25%;height:0;overflow:hidden;border-radius:12px">
      <iframe src="{src}" title="YouTube clip preview" style="position:absolute;top:0;left:0;width:100%;height:100%;border:0"
      allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen></iframe>
    </div>
    """,height=390)

if "candidates" not in st.session_state: st.session_state.candidates=[]
if "video_id" not in st.session_state: st.session_state.video_id=""
if "transcript_rows" not in st.session_state: st.session_state.transcript_rows=[]
if "chosen" not in st.session_state: st.session_state.chosen=0
if "analysis_complete" not in st.session_state: st.session_state.analysis_complete=False
if "upload_queue" not in st.session_state: st.session_state.upload_queue=[]
if "audience_feedback" not in st.session_state: st.session_state.audience_feedback=[]

left,right=st.columns([1.1,0.9],gap="large")
with left:
    st.subheader("1 · Paste a YouTube URL")
    url=st.text_input("YouTube video URL",placeholder="https://www.youtube.com/watch?v=...")
    lang=st.selectbox("Preferred transcript language",["en","hi","mr","es","pt","fr","de"],index=0)
    transcript_upload=st.file_uploader("Transcript fallback (.srt, .vtt, or .txt)",type=["srt","vtt","txt"],help="Use this when captions are unavailable or YouTube blocks transcript retrieval. SRT/VTT keeps supplied timestamps; plain text receives estimated timestamps.")
    if st.button("Get transcript & find meme moments",type="primary",use_container_width=True):
        vid=parse_video_id(url)
        if not vid:
            st.error("That doesn't look like a supported YouTube video URL.")
        else:
            try:
                st.session_state.analysis_complete=False
                st.session_state.candidates=[]
                LOGGER.info("transcript_fetch_started video_id=%s",vid)
                if transcript_upload is not None:
                    with st.spinner("Reading uploaded transcript…"):
                        rows=parse_transcript_file(transcript_upload)
                    used_lang=lang
                    generated=None
                    if transcript_upload.name.lower().endswith(".txt"):
                        st.info("Plain-text transcript timestamps are estimates based on reading speed. Upload SRT/VTT for source-aligned timings.")
                else:
                    with st.spinner("Fetching available captions/transcript…"):
                        rows,used_lang,generated=fetch_transcript(vid,lang)
                if not rows: raise RuntimeError("Transcript was empty.")
                LOGGER.info("transcript_ready video_id=%s source=%s segments=%d language=%s",vid,"upload" if transcript_upload is not None else "youtube",len(rows),used_lang)
                st.session_state.video_id=vid
                st.session_state.transcript_rows=rows
                st.session_state.transcript_language=used_lang
                progress_box=st.empty()
                progress_lines=[]
                agent_names={"ingestion":"Ingestion agent","comedic_structure":"Comedic structure agent","reaction_signal":"Reaction signal agent","candidate_generation":"Clip candidate agent","shareability_ranking":"Shareability ranking agent","meme_caption":"Meme caption agent"}
                with st.status("Starting multi-agent analysis…",expanded=True) as run_status:
                    def show_agent_progress(event):
                        agent=event.get("agent","agent")
                        message=event.get("message","Working…")
                        label=agent_names.get(agent,agent.replace("_"," ").title())
                        progress_lines.append(f"{label}: {message}")
                        run_status.update(label=f"{label} · {message}",state="running")
                        progress_box.code("\n".join(progress_lines[-18:]))
                    candidates=make_candidates(rows,used_lang,max_candidates,target_len,show_agent_progress)
                    run_status.update(label="All agents finished.",state="complete")
                duration=max(r["end"] for r in rows)
                valid=[]
                for c in candidates:
                    try:
                        s=max(0,float(c["start"])); e=min(duration,float(c["end"]))
                        if e-s<3: e=min(duration,s+3)
                        if e-s>8: e=s+8
                        if e-s>=2.5:
                            c["start"]=s; c["end"]=e
                            c["score"]=max(0,min(100,float(c.get("score",0))))
                            valid.append(c)
                    except Exception: pass
                st.session_state.candidates=sorted(valid,key=lambda x:x.get("score",0),reverse=True)
                st.session_state.chosen=0
                st.session_state.analysis_complete=True
                LOGGER.info("analysis_complete video_id=%s candidates=%d",vid,len(valid))
                st.success(f"Fetched {len(rows)} transcript segments · language {used_lang} · {len(valid)} candidate moments")
            except Exception as e:
                LOGGER.error("analysis_failed video_id=%s error_type=%s",vid,type(e).__name__)
                message=str(e)
                if "Could not retrieve a transcript" in message or "NoTranscriptFound" in message or "TranscriptsDisabled" in message:
                    st.error("YouTube did not provide captions for this video.")
                elif "ModuleNotFoundError" in message or "No module named" in message:
                    st.error("LangGraph dependencies are missing. From the project folder, run: pip install -r requirements.txt")
                else:
                    st.error(f"Could not complete transcript analysis: {message[:500]}")
                st.caption("Captions can be disabled, age/region restricted, or blocked. Upload an SRT/VTT/TXT transcript above to continue. The official YouTube captions download API requires owner/editor access.")

with right:
    st.subheader("2 · Source video")
    if st.session_state.video_id:
        st.markdown(f"[Open original on YouTube](https://www.youtube.com/watch?v={st.session_state.video_id})")
        embed_window(st.session_state.video_id,0,3600)
    else:
        st.info("Paste a URL and run analysis. The source player will appear here.")

def creator_rank(candidates):
    """Small transparent feedback adjustment from manually entered share counts."""
    feedback=st.session_state.audience_feedback
    if len(feedback)<3: return [dict(c) for c in candidates]
    rates=[item["shares"]/max(1,item["views"]) for item in feedback]
    overall=sum(rates)/len(rates)
    type_rates={}
    for item in feedback:
        type_rates.setdefault(item["joke_type"],[]).append(item["shares"]/max(1,item["views"]))
    result=[]
    for original in candidates:
        c=dict(original)
        values=type_rates.get(c.get("joke_type","moment"),[])
        adjustment=0
        if len(values)>=2:
            adjustment=max(-8,min(8,round((sum(values)/len(values)-overall)*1000)))
        c["creator_adjustment"]=adjustment
        c["creator_score"]=max(0,min(100,float(c.get("score",0))+adjustment))
        result.append(c)
    return sorted(result,key=lambda x:x.get("creator_score",x.get("score",0)),reverse=True)

def safe_caption_html(value):
    safe=html.escape(str(value)).replace("\n","<br>")
    safe=re.sub(r"\*\*(.+?)\*\*",r"<strong>\1</strong>",safe)
    safe=re.sub(r"\*(.+?)\*",r"<em>\1</em>",safe)
    return safe

st.divider()
st.markdown("### Studio workspace")
if not st.session_state.candidates:
    if st.session_state.analysis_complete:
        st.warning("Analysis completed, but no clip candidates were produced. Check the agent activity above and logs/ps18.log for per-chunk model errors and candidate counts.")
    else:
        st.info("Add a YouTube URL and analyze its transcript to fill the Studio workspace.")
moments=creator_rank(st.session_state.candidates)
tab_moments,tab_meme,tab_publish,tab_feedback,tab_pipeline=st.tabs(["📍 Moments","🎨 Meme Studio","🚀 Publish queue","📈 Audience feedback","⚙️ Pipeline coverage"])

with tab_moments:
    if moments:
        count=len(moments)
        duration=max((r.get("end",0) for r in st.session_state.transcript_rows),default=0)
        avg_score=round(sum(c.get("creator_score",c.get("score",0)) for c in moments)/count)
        metric_cols=st.columns(4)
        metrics=[("MOMENTS FOUND",str(count)),("SOURCE LENGTH",fmt_time(duration)),("AVG. SCORE",f"{avg_score}/100"),("SOURCE","YouTube transcript")]
        for col,(label,value) in zip(metric_cols,metrics):
            col.markdown(f'<div class="metric-card"><div class="metric-label">{label}</div><div class="metric-value">{value}</div></div>',unsafe_allow_html=True)
        st.markdown("#### All detected comedy moments")
        table=[{"#":i+1,"Timestamp":f"{fmt_time(c['start'])} – {fmt_time(c['end'])}","Length":f"{c['end']-c['start']:.1f}s","Score":f"{c.get('creator_score',c.get('score',0)):.0f}","Type":c.get("joke_type","moment"),"Reaction clue":c.get("reaction_signal","none")} for i,c in enumerate(moments)]
        st.dataframe(table,use_container_width=True,hide_index=True)
        with st.expander("Jump to any moment"):
            for i,c in enumerate(moments):
                left_col,right_col=st.columns([5,1])
                left_col.markdown(f"**{i+1:02d} · {fmt_time(c['start'])} → {fmt_time(c['end'])}**　`{c.get('joke_type','moment')}`　·　{c.get('creator_score',c.get('score',0))}/100")
                right_col.link_button("Open moment",f"https://www.youtube.com/watch?v={st.session_state.video_id}&t={int(c['start'])}s",use_container_width=True)
                st.caption(str(c.get("evidence",""))[:260])
        with st.expander("Inspect transcript"):
            st.text_area("Timestamped transcript","\n".join(f"[{fmt_time(r['start'])}] {r['text']}" for r in st.session_state.transcript_rows),height=260)
        export={"video_id":st.session_state.video_id,"moments":moments}
        st.download_button("Download all moments (JSON)",json.dumps(export,ensure_ascii=False,indent=2),file_name="ps18_all_moments.json",mime="application/json")
    else:
        st.caption("Detected timestamps, scores, joke types, and transcript clues appear here.")

with tab_meme:
    if moments:
        options=list(range(len(moments)))
        selected=st.selectbox("Choose a detected moment",options,format_func=lambda i:f"#{i+1} · {fmt_time(moments[i]['start'])}–{fmt_time(moments[i]['end'])} · {moments[i].get('joke_type','moment')}",key="meme_moment")
        c=moments[selected]
        left_col,right_col=st.columns([1.05,0.95],gap="large")
        with left_col:
            st.markdown(f"#### Source preview · {fmt_time(c['start'])}–{fmt_time(c['end'])}")
            embed_window(st.session_state.video_id,c["start"],c["end"])
            st.link_button("Open exact timestamp on YouTube",f"https://www.youtube.com/watch?v={st.session_state.video_id}&t={int(c['start'])}s")
            st.caption(f"{c['end']-c['start']:.1f}s · {c.get('joke_type','comedy moment')} · reaction clue: {c.get('reaction_signal','none')}")
            st.markdown("**Transcript proof**")
            st.info(c.get("evidence","No transcript excerpt available."))
        with right_col:
            st.markdown("#### Meme builder")
            caps=c.get("captions",{})
            style=st.selectbox("Caption style",["meme","deadpan","hype"],format_func=lambda x:{"meme":"Gen-Z meme","deadpan":"Deadpan","hype":"Hype"}[x],key=f"style_{selected}")
            default_caption=caps.get(style) or c.get("overlay","") or "POV: the story took an unexpected turn 💀"
            editor_key=f"caption_edit_{selected}_{style}"
            if editor_key not in st.session_state: st.session_state[editor_key]=default_caption
            caption=st.text_area("Caption · Markdown supported",key=editor_key,height=90)
            sticker=st.selectbox("Reaction sticker",["💀","😂","😭","🤯","🍿","🔥","👀"],key=f"sticker_{selected}")
            overlay=st.text_input("On-screen overlay",value=c.get("overlay","") or caption,key=f"overlay_{selected}")
            st.markdown(f'<div class="meme-card"><div class="meme-kicker">PS-18 · {html.escape(str(c.get("joke_type","COMEDY MOMENT")).upper())}</div><div class="meme-caption">{safe_caption_html(caption)}　{sticker}</div><div class="meme-evidence">ON-SCREEN · {safe_caption_html(overlay)}</div></div>',unsafe_allow_html=True)
            st.markdown("**Caption variants**")
            st.markdown(f"- 😐 **Deadpan:** {caps.get('deadpan','—')}\n- 🚀 **Hype:** {caps.get('hype','—')}\n- 💀 **Meme:** {caps.get('meme','—')}")
            markdown_export=f"# {sticker} {caption}\n\n**Moment:** {fmt_time(c['start'])}–{fmt_time(c['end'])}\n\n**Overlay:** {overlay}\n\n**Evidence:** {c.get('evidence','')}\n"
            st.download_button("Export meme card (Markdown)",markdown_export,file_name=f"ps18_meme_{fmt_time(c['start']).replace(':','-')}.md",mime="text/markdown",key=f"meme_export_{selected}")
            st.caption("Preview is a meme card and caption export. The source video is not rendered or cropped into an MP4 yet.")
    else:
        st.info("Run analysis to create meme cards from detected moments.")

with tab_publish:
    st.markdown("#### Upload scheduler · mock mode")
    channel_id=os.getenv("YOUTUBE_CHANNEL_ID","").strip()
    if channel_id:
        st.success(f"Channel loaded from YOUTUBE_CHANNEL_ID: `{channel_id}`")
    else:
        st.warning("Set YOUTUBE_CHANNEL_ID in your local .env to attach a creator channel to mock queue items.")
    st.caption("Queue entries are session-local simulations. No API request or video upload is performed.")
    if moments:
        with st.form("mock_publish_form"):
            publish_idx=st.selectbox("Moment to queue",range(len(moments)),format_func=lambda i:f"#{i+1} · {fmt_time(moments[i]['start'])}–{fmt_time(moments[i]['end'])}")
            platform=st.selectbox("Destination",["YouTube Shorts (mock)","TikTok (mock)","Instagram Reels (mock)"])
            suggested=f"{moments[publish_idx].get('captions',{}).get('meme','Comedy moment')} #shorts"
            title=st.text_input("Short title",value=suggested[:95])
            schedule_date=st.date_input("Publish date",value=date.today())
            schedule_time=st.time_input("Publish time",value=dt_time(hour=18,minute=0))
            reviewed=st.checkbox("I reviewed the clip, rights, and caption for this mock publish")
            submit=st.form_submit_button("Add to mock publishing queue",type="primary",use_container_width=True)
        if submit:
            if not reviewed:
                st.error("Review and check the rights box before queueing.")
            else:
                moment=moments[publish_idx]
                st.session_state.upload_queue.append({"id":f"MOCK-{len(st.session_state.upload_queue)+1:04d}","platform":platform,"channel_id":channel_id or "not configured","video_id":st.session_state.video_id,"start":moment["start"],"end":moment["end"],"title":title,"scheduled_at":datetime.combine(schedule_date,schedule_time).isoformat(timespec="minutes"),"status":"Ready for human review","mode":"SIMULATION"})
                LOGGER.info("mock_upload_queued platform=%s channel_configured=%s",platform,bool(channel_id))
                st.success("Added a simulated upload. No video was uploaded.")
    else:
        st.info("Detect moments first to add one to the queue.")
    st.markdown("#### Queue")
    if st.session_state.upload_queue:
        for entry in reversed(st.session_state.upload_queue):
            with st.container(border=True):
                a,b=st.columns([5,1])
                a.markdown(f"**{entry['id']} · {entry['status']}**　<span class='studio-chip'>{entry['mode']}</span>",unsafe_allow_html=True)
                a.write(f"{entry['platform']} · {entry['scheduled_at']} · channel `{entry['channel_id']}`")
                a.caption(f"{fmt_time(entry['start'])}–{fmt_time(entry['end'])} · {entry['title']}")
                if b.button("Remove",key=f"remove_{entry['id']}"):
                    st.session_state.upload_queue=[q for q in st.session_state.upload_queue if q['id']!=entry['id']]
                    st.rerun()
    else:
        st.caption("No queued items yet.")

with tab_feedback:
    st.markdown("#### Creator audience signals")
    st.caption("Enter performance from published clips. After 3 records, joke types with stronger share rates receive a small transparent ranking adjustment.")
    with st.form("feedback_form"):
        f1,f2,f3,f4=st.columns(4)
        joke_type=f1.selectbox("Joke type",sorted({c.get('joke_type','moment') for c in moments}) or ["deadpan","callback","banter","reaction"])
        views=f2.number_input("Views",min_value=0,value=1000,step=100)
        shares=f3.number_input("Shares",min_value=0,value=20,step=1)
        likes=f4.number_input("Likes",min_value=0,value=100,step=10)
        save_feedback=st.form_submit_button("Save creator feedback")
    if save_feedback:
        if shares>views:
            st.error("Shares cannot exceed views.")
        else:
            st.session_state.audience_feedback.append({"joke_type":joke_type,"views":int(views),"shares":int(shares),"likes":int(likes)})
            LOGGER.info("audience_feedback_saved joke_type=%s views=%d shares=%d",joke_type,int(views),int(shares))
            st.success("Saved locally for this session. Moment scores update from the 3rd feedback record onward.")
    records=st.session_state.audience_feedback
    if records:
        total_views=sum(x["views"] for x in records); total_shares=sum(x["shares"] for x in records)
        x1,x2,x3=st.columns(3)
        x1.metric("Feedback records",len(records)); x2.metric("Views logged",f"{total_views:,}"); x3.metric("Share rate",f"{(100*total_shares/max(1,total_views)):.2f}%")
        st.dataframe(records,use_container_width=True,hide_index=True)
    else:
        st.info("No audience performance has been entered for this session.")

with tab_pipeline:
    st.markdown("#### PS-18 feature coverage")
    coverage=[
        ("Long-form transcript ingestion + chunking","LIVE"),("Comedic structure + many timestamps","LIVE"),("Reaction cues from transcript","LIVE · TEXT ONLY"),("Ranking + joke-type labels","LIVE"),("Caption A/B + Markdown meme cards","LIVE"),("Moment-by-moment source preview","LIVE"),("Audio/visual laughter and delivery cues","NOT IMPLEMENTED"),("Vertical MP4 render + loop trimming","NOT IMPLEMENTED"),("Rights/music safety pre-check","MANUAL REVIEW"),("Scheduled publishing","MOCK QUEUE"),("Creator audience learning","SESSION-LOCAL FEEDBACK"),("Cross-platform live publish + analytics","NOT IMPLEMENTED"),("Weekly Best Of compilation","NOT IMPLEMENTED")]
    for start in range(0,len(coverage),3):
        cols=st.columns(3)
        for col,(name,status) in zip(cols,coverage[start:start+3]):
            color="#50d890" if status=="LIVE" else "#efb64b" if "MOCK" in status or "MANUAL" in status or "SESSION" in status else "#adb1bb"
            col.markdown(f'<div class="metric-card"><div class="metric-label">{html.escape(status)}</div><div class="metric-value" style="font-size:15px">{html.escape(name)}</div><div style="height:3px;background:{color};margin-top:12px;border-radius:3px"></div></div>',unsafe_allow_html=True)
    st.caption("A YouTube channel ID is read from YOUTUBE_CHANNEL_ID. OAuth upload and platform APIs are intentionally not called in this mock build.")

st.divider()
st.caption("Studio scores are heuristics, not virality predictions. Transcript reaction words are proxies; audio and visual reactions are not analyzed. Queue and feedback data are local to the current Streamlit session.")
