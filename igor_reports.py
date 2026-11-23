"""Portable HTML graph reports. The recipient needs only a modern browser."""

import base64
import html
import hashlib
import json
import math
from pathlib import Path
import re

FORMAT = "igor-editable-report-v1"
MARKER = '<script id="igor-report-data" type="application/json">'
MAX_BYTES = 50_000_000
MAX_POINTS = 10_000
MAX_TOTAL_POINTS = 100_000
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,30}\Z")
TRACE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,30}(?:#[0-9]+)?\Z")


def data_fingerprint(x, y):
    """Hash IEEE numeric values independently of JSON's integer/float spelling."""
    def values(array):
        return ",".join("null" if v is None else "0" if v == 0 else format(float(v), ".17g") for v in array)
    return hashlib.sha256((values(x) + "|" + values(y)).encode("ascii")).hexdigest()


def text(value, label, limit=10_000, *, single_line=False):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"Invalid {label}: expected text up to {limit} characters")
    if any(ord(c) < 32 and (single_line or c not in "\n\r\t") for c in value):
        raise ValueError(f"Invalid control character in {label}")
    return value


def number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e300:
        raise ValueError(f"Invalid finite number for {label}")
    return value


def validate(data):
    """Validate untrusted downloaded HTML data before any possible Igor apply."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("Not an Igor editable HTML report")
    for key in ("title", "notes", "source_experiment"):
        text(data.get(key), key, 100_000)
    figures = data.get("figures")
    if not isinstance(figures, list) or not 1 <= len(figures) <= 20:
        raise ValueError("Report must contain 1 to 20 figures")
    names, total = set(), 0
    for figure in figures:
        if not isinstance(figure, dict):
            raise ValueError("Invalid figure")
        graph = figure.get("graph")
        if not isinstance(graph, str) or not NAME.fullmatch(graph) or graph in names:
            raise ValueError("Figures require unique portable top-level graph names")
        names.add(graph)
        for key in ("title", "caption"):
            text(figure.get(key), key)
        if type(figure.get("edited")) is not bool or type(figure.get("legend")) is not bool:
            raise ValueError("Invalid figure display flags")
        preview = figure.get("original_png")
        if not isinstance(preview, str) or not base64.b64decode(preview, validate=True).startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Invalid original PNG")
        for axis in ("x", "y"):
            settings = figure.get(axis)
            if not isinstance(settings, dict):
                raise ValueError("Invalid axis")
            text(settings.get("label"), "axis label", 1000, single_line=True)
            lo, hi = number(settings.get("min"), "axis minimum"), number(settings.get("max"), "axis maximum")
            if lo == hi or type(settings.get("log")) is not bool or (settings["log"] and min(lo, hi) <= 0):
                raise ValueError("Axis limits must differ and log limits must be positive")
        traces = figure.get("traces")
        if not isinstance(traces, list) or not 1 <= len(traces) <= 20:
            raise ValueError("A figure must contain 1 to 20 traces")
        trace_names = set()
        for trace in traces:
            if not isinstance(trace, dict) or not isinstance(trace.get("name"), str) or not TRACE.fullmatch(trace["name"]) or trace["name"] in trace_names:
                raise ValueError("Invalid or duplicate trace name")
            trace_names.add(trace["name"])
            for key in ("label", "y_wave", "x_wave", "data_sha256"):
                text(trace.get(key), key, 1000, single_line=True)
            if not re.fullmatch(r"[a-f0-9]{64}", trace["data_sha256"]):
                raise ValueError("Invalid source data fingerprint")
            if not isinstance(trace.get("color"), str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", trace["color"]):
                raise ValueError("Invalid trace color")
            if not 0 < number(trace.get("width"), "trace width") <= 10:
                raise ValueError("Trace width must be in (0, 10]")
            if type(trace.get("mode")) is not int or trace["mode"] not in (0, 2, 3, 4):
                raise ValueError("Only line, dot, marker, and line-with-marker traces are supported")
            x, y = trace.get("x"), trace.get("y")
            if not isinstance(x, list) or not isinstance(y, list) or not 1 <= len(x) == len(y) <= MAX_POINTS:
                raise ValueError("Trace arrays must have equal lengths, from 1 to 10,000")
            total += len(x)
            if total > MAX_TOTAL_POINTS:
                raise ValueError("Report exceeds 100,000 total data points")
            for value in x + y:
                if value is not None:
                    number(value, "data point")
    return data


def read_report(path):
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Report exceeds 50 MB")
    source = path.read_text(encoding="utf-8")
    if source.count(MARKER) != 1:
        raise ValueError("Not an Igor editable HTML report")
    encoded = source.split(MARKER, 1)[1].split("</script>", 1)[0]
    return validate(json.loads(encoded))


def render_report(data):
    validate(data)
    encoded = json.dumps(data, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return TEMPLATE.replace("<!--TITLE-->", html.escape(data["title"])).replace("<!--DATA-->", MARKER + encoded + "</script>")


def write_report(path, data):
    content = render_report(data).encode("utf-8")
    if len(content) > MAX_BYTES:
        raise ValueError("Report exceeds 50 MB")
    with Path(path).open("xb") as stream:
        stream.write(content)


TEMPLATE = r'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; base-uri 'none'; form-action 'none'">
<title><!--TITLE--></title>
<style>
:root{font-family:system-ui,sans-serif;color:#20333f;background:#edf2f5;font-size:16px}
body{margin:0}main{max-width:1000px;margin:32px auto;padding:32px;background:white;border-radius:12px}
h1{font-size:32px;line-height:1.2}p{overflow-wrap:anywhere}.hint,small{color:#536977;font-size:14px}
button,input,textarea,select{font:inherit}button{border:1px solid #aebfcb;border-radius:6px;padding:8px 12px;background:#edf5fa;color:#173f5d;cursor:pointer}
button.primary{background:#173f5d;color:white}button:hover{filter:brightness(.94)}
.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center}.toolbar.top{position:sticky;top:0;background:white;padding:12px 0;z-index:2;border-bottom:1px solid #dce5eb}
#notes{white-space:pre-wrap}.figure{margin:28px 0 44px;border:1px solid #dce5eb;border-radius:8px;padding:18px}
.plot{width:100%;cursor:pointer}.plot svg,.plot img{display:block;width:100%;height:auto}.caption{white-space:pre-wrap}
.editor{background:#f5f8fa;padding:16px;margin-top:16px;border-radius:6px}.editor[hidden]{display:none}
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:12px;margin-bottom:16px}
label{display:flex;flex-direction:column;font-size:14px;gap:4px}input,textarea,select{box-sizing:border-box;width:100%;padding:6px;border:1px solid #bccbd4;border-radius:4px;background:white}
input[type=checkbox]{width:auto}input[type=color]{height:36px}.trace{border-top:1px solid #cedce5;padding:12px 0}
.error{color:#a52621;min-height:1.5em}#status{font-size:14px}.source{font-size:12px;color:#536977}
@media(max-width:650px){main{margin:0;padding:16px}.figure{padding:8px}}
@media print{body{background:white}main{margin:0;padding:0}.toolbar,.editor,.hint,.error{display:none}.figure{break-inside:avoid;border:0;padding:0}}
</style></head><body><main>
<div class="toolbar top"><button id="save" class="primary">Save edited HTML</button><button id="editReport">Edit report text</button><span id="status" role="status">Ready. Click a figure to edit it.</span></div>
<h1 id="heading"></h1><p id="notes"></p>
<div id="reportEditor" class="editor" hidden><label>Report title<input id="reportTitle" maxlength="100000"></label><label>Notes<textarea id="reportNotes" rows="5" maxlength="100000"></textarea></label><button id="applyText">Apply text</button></div>
<p class="hint">Open and edit this file in your browser. No installation, Igor, MCP, AI, or internet connection is needed.
Save edited HTML downloads a new editable copy; your browser may ask where to save it. The source Igor experiment is not changed.</p>
<div id="figures"></div><p class="source" id="source"></p>
<!--DATA-->
<script>
'use strict';
const data = JSON.parse(document.getElementById('igor-report-data').textContent);
let dirty = false;
const pendingFigures = new Set();
let pendingText = false;
const NS = 'http://www.w3.org/2000/svg';
const status = document.getElementById('status');
const el = (tag, text, className) => {const n=document.createElement(tag); if(text!==undefined)n.textContent=text;if(className)n.className=className;return n;};
function svgEl(tag, attributes, text) {const n=document.createElementNS(NS,tag);for(const [k,v] of Object.entries(attributes))n.setAttribute(k,String(v));if(text!==undefined)n.textContent=text;return n;}
function markDirty(){dirty=true;status.textContent='Unsaved changes. Choose Save edited HTML to keep them.';}
function axisRange(axis) {
  const lo=Number(axis.min),hi=Number(axis.max);
  if(!Number.isFinite(lo)||!Number.isFinite(hi)||Math.abs(lo)>1e300||Math.abs(hi)>1e300||lo===hi||(axis.log&&Math.min(lo,hi)<=0))throw Error('Axis limits must be finite and different; log limits must be positive.');
  return [lo,hi];
}
function draw(figure,index) {
  const W=900,H=570,L=100,R=30,T=55,B=85,pw=W-L-R,ph=H-T-B;
  const svg=svgEl('svg',{xmlns:NS,viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':figure.title});
  svg.append(svgEl('rect',{width:W,height:H,fill:'white'}));
  const add=(tag,a,t)=>{const n=svgEl(tag,a,t);svg.append(n);return n;};
  const trans=(v,axis)=>axis.log?Math.log10(v):v;
  const bounds={};for(const key of ['x','y']){const range=axisRange(figure[key]);bounds[key]=range.map(v=>trans(v,figure[key]));}
  const scale=(v,key)=>{const [lo,hi]=bounds[key];const ratio=(trans(v,figure[key])/2-lo/2)/(hi/2-lo/2);return key==='x'?L+ratio*pw:T+ph-ratio*ph;};
  const labelStyle={'font-family':'Arial, sans-serif',fill:'#20333f','font-size':15};
  const fmt=v=>v===0?'0':(Math.abs(v)>=1e5||Math.abs(v)<.001? v.toExponential(2):Number(v.toPrecision(5)).toString());
  for(let i=0;i<=5;i++){
    for(const key of ['x','y']){
      const [lo,hi]=bounds[key],t=lo*(1-i/5)+hi*(i/5),value=figure[key].log?10**t:t;
      const p=key==='x'?L+i*pw/5:T+ph-i*ph/5;
      if(key==='x'){add('line',{x1:p,x2:p,y1:T,y2:T+ph,stroke:'#e1e8ed'});add('text',{x:p,y:T+ph+26,'text-anchor':'middle',...labelStyle},fmt(value));}
      else{add('line',{x1:L,x2:L+pw,y1:p,y2:p,stroke:'#e1e8ed'});add('text',{x:L-12,y:p+5,'text-anchor':'end',...labelStyle},fmt(value));}
    }
  }
  add('rect',{x:L,y:T,width:pw,height:ph,fill:'none',stroke:'#20333f'});
  const defs=add('defs',{}),clip=svgEl('clipPath',{id:`clip${index}`});clip.append(svgEl('rect',{x:L,y:T,width:pw,height:ph}));defs.append(clip);
  const group=add('g',{'clip-path':`url(#clip${index})`});
  figure.traces.forEach(trace=>{
    let path='',pen=false;
    for(let i=0;i<trace.x.length;i++){
      const x=trace.x[i],y=trace.y[i];
      if(x===null||y===null||!Number.isFinite(x)||!Number.isFinite(y)||(figure.x.log&&x<=0)||(figure.y.log&&y<=0)){pen=false;continue;}
      const sx=scale(x,'x'),sy=scale(y,'y');if(!Number.isFinite(sx)||!Number.isFinite(sy)){pen=false;continue;}
      if(trace.mode===0||trace.mode===4){path+=(pen?'L':'M')+sx.toFixed(3)+','+sy.toFixed(3);pen=true;}
      if(trace.mode!==0)group.append(svgEl('circle',{cx:sx,cy:sy,r:trace.mode===2?1.4:3,fill:trace.color}));
    }
    if(path)group.append(svgEl('path',{d:path,fill:'none',stroke:trace.color,'stroke-width':trace.width,'stroke-linejoin':'round'}));
  });
  add('text',{x:L+pw/2,y:28,'text-anchor':'middle',...labelStyle,'font-size':21},figure.title);
  add('text',{x:L+pw/2,y:H-20,'text-anchor':'middle',...labelStyle,'font-size':18},figure.x.label);
  add('text',{transform:`translate(25 ${T+ph/2}) rotate(-90)`,'text-anchor':'middle',...labelStyle,'font-size':18},figure.y.label);
  if(figure.legend){
    const width=Math.min(380,Math.max(110,...figure.traces.map(t=>t.label.length*8+55)));
    add('rect',{x:W-R-width-8,y:T+8,width,height:figure.traces.length*23+12,fill:'white',stroke:'#c6d2db',opacity:.95});
    figure.traces.forEach((trace,i)=>{const y=T+29+i*23;add('line',{x1:W-R-width+2,x2:W-R-width+25,y1:y-5,y2:y-5,stroke:trace.color,'stroke-width':trace.width});add('text',{x:W-R-width+33,y,...labelStyle},trace.label);});
  }
  return svg;
}
function field(parent,label,value,type='text'){
  const wrapper=el('label',label),input=el('input');input.type=type;
  if(type==='checkbox')input.checked=value;else input.value=value;
  if(type==='text')input.maxLength=1000;
  if(type==='number')input.step='any';
  wrapper.append(input);parent.append(wrapper);return input;
}
function download(content,type,name){const url=URL.createObjectURL(new Blob([content],{type}));const a=el('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);}
function safeName(name){return name.replace(/[^a-z0-9_-]+/gi,'_').slice(0,80)||'report';}
function renderFigures(){
  const root=document.getElementById('figures');root.replaceChildren();
  data.figures.forEach((figure,index)=>{
    const section=el('section',undefined,'figure'),heading=el('h2',figure.title),plot=el('div',undefined,'plot');
    plot.tabIndex=0;plot.setAttribute('role','button');plot.setAttribute('aria-label','Edit '+figure.title);
    const caption=el('p',figure.caption,'caption'),bar=el('div',undefined,'toolbar'),editor=el('div',undefined,'editor');editor.hidden=true;
    const error=el('p','','error');error.setAttribute('role','alert');
    const display=()=>{plot.replaceChildren();if(figure.edited)plot.append(draw(figure,index));else{const img=el('img');img.src='data:image/png;base64,'+figure.original_png;img.alt=figure.title;plot.append(img);}};
    display();
    const toggle=el('button','Edit figure'),original=el('button','Show original Igor preview'),exportSVG=el('button','Download editable rendering as SVG');
    toggle.onclick=()=>{editor.hidden=!editor.hidden;};plot.onclick=toggle.onclick;plot.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle.onclick();}};
    original.onclick=()=>{figure.edited=!figure.edited;display();markDirty();};
    exportSVG.onclick=()=>{try{download(new XMLSerializer().serializeToString(draw(figure,index)),'image/svg+xml',safeName(figure.title)+'.svg');}catch(e){error.textContent=e.message;}};
    bar.append(toggle,original,exportSVG);
    const hint=el('p','Browser editing uses a simplified XY rendering. The original Igor preview is retained for reference. Symbols, fonts, annotations, and custom ticks may look different.','hint');editor.append(hint);
    const general=el('div',undefined,'fields');editor.append(general);
    const title=field(general,'Figure title',figure.title),cap=field(general,'Caption',figure.caption),legend=field(general,'Show legend',figure.legend,'checkbox');
    const axes={};
    for(const key of ['x','y']){
      const fields=el('div',undefined,'fields');editor.append(el('h3',key.toUpperCase()+' axis'),fields);
      axes[key]={label:field(fields,'Label',figure[key].label),min:field(fields,'Minimum',figure[key].min,'number'),max:field(fields,'Maximum',figure[key].max,'number'),log:field(fields,'Logarithmic',figure[key].log,'checkbox')};
    }
    const traceFields=figure.traces.map(trace=>{
      const fields=el('div',undefined,'fields trace');editor.append(el('h3',trace.name),fields);
      const label=field(fields,'Legend label',trace.label),color=field(fields,'Color',trace.color,'color'),width=field(fields,'Line width',trace.width,'number');width.min=.1;width.max=10;
      const wrapper=el('label','Display'),mode=el('select');for(const [value,text] of [[0,'Line'],[2,'Dots'],[3,'Markers'],[4,'Line + markers']]){const option=el('option',text);option.value=value;mode.append(option);}mode.value=trace.mode;wrapper.append(mode);fields.append(wrapper);
      return {label,color,width,mode};
    });
    const apply=el('button','Apply figure changes','primary');
    apply.onclick=()=>{
      try{
        const next=JSON.parse(JSON.stringify(figure));next.title=title.value;next.caption=cap.value;next.legend=legend.checked;
        for(const key of ['x','y']){const a=axes[key];if(a.min.value.trim()===''||a.max.value.trim()==='')throw Error('Enter both axis limits.');next[key]={label:a.label.value,min:Number(a.min.value),max:Number(a.max.value),log:a.log.checked};axisRange(next[key]);}
        traceFields.forEach((fields,i)=>{const width=Number(fields.width.value);if(!Number.isFinite(width)||width<=0||width>10)throw Error('Line width must be greater than zero and no more than 10.');Object.assign(next.traces[i],{label:fields.label.value,color:fields.color.value,width,mode:Number(fields.mode.value)});});
        next.edited=true;draw(next,index);Object.assign(figure,next);heading.textContent=figure.title;caption.textContent=figure.caption;display();error.textContent='';pendingFigures.delete(index);markDirty();
      }catch(e){error.textContent=e.message;}
    };
    editor.addEventListener('input',()=>{pendingFigures.add(index);markDirty();});
    editor.append(apply);section.append(heading,plot,caption,bar,editor,error);root.append(section);
  });
}
function renderText(){document.title=data.title;document.getElementById('heading').textContent=data.title;document.getElementById('notes').textContent=data.notes;}
document.getElementById('editReport').onclick=()=>{const editor=document.getElementById('reportEditor');editor.hidden=!editor.hidden;document.getElementById('reportTitle').value=data.title;document.getElementById('reportNotes').value=data.notes;};
document.getElementById('reportEditor').addEventListener('input',()=>{pendingText=true;markDirty();});
document.getElementById('applyText').onclick=()=>{data.title=document.getElementById('reportTitle').value;data.notes=document.getElementById('reportNotes').value;renderText();pendingText=false;markDirty();};
document.getElementById('save').onclick=()=>{
  if(pendingFigures.size||pendingText){status.textContent='Apply your pending figure or text changes before saving.';return;}
  // Only the validated data block is authoritative when the downloaded file reopens.
  document.getElementById('igor-report-data').textContent=JSON.stringify(data).replace(/</g,'\\u003c').replace(/>/g,'\\u003e').replace(/&/g,'\\u0026');
  const clone=document.documentElement.cloneNode(true);clone.querySelector('#figures').replaceChildren();clone.querySelector('#reportEditor').hidden=true;
  clone.querySelector('#status').textContent='Ready. Click a figure to edit it.';
  download('<!DOCTYPE html>\n'+clone.outerHTML,'text/html;charset=utf-8',safeName(data.title)+'_edited.html');dirty=false;status.textContent='Edited HTML downloaded. Open the saved copy to continue or send it to someone else.';
};
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
document.getElementById('source').textContent='Source: '+(data.source_experiment||'Unsaved Igor experiment')+'. Numerical data are included in this file. Browser edits change presentation only.';
renderText();renderFigures();
</script></main></body></html>'''
