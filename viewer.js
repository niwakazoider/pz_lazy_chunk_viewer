(() => {
'use strict';

const canvas = document.getElementById('map');
const ctx = canvas.getContext('2d');
const wrap = document.getElementById('mapWrap');
const hud = document.getElementById('hud');

const state = {
  cfg:null,
  startX:1329,
  startY:1300,
  z:0,
  mode:'static',
  zoom:1,
  opacity:.70,
  cameraX:0,
  cameraY:0,
  dragging:false,
  dragX:0,
  dragY:0,
  showGrid:true,
  generation:0,
  player:null,
  currentSave:null,
  saveList:[],
};

const layers = {
  static:new Map(),
  save:new Map(),
};
const queue = [];
const queued = new Set();   // waiting, not started yet
const loading = new Set();  // fetch/image load currently in progress
let workers = 0;
const MAX_WORKERS = 10;

function key(x,y,z){ return `${x},${y},${z}`; }

function resize(){
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const r = wrap.getBoundingClientRect();
  canvas.width = Math.max(1, Math.floor(r.width*dpr));
  canvas.height = Math.max(1, Math.floor(r.height*dpr));
  canvas.style.width = r.width+'px';
  canvas.style.height = r.height+'px';
  ctx.setTransform(dpr,0,0,dpr,0,0);
  draw();
  refreshWanted();
}

function chunkBase(dx,dy){
  return [(dx-dy)*512, (dx+dy)*256];
}

function inverseChunk(px,py){
  return [
    (px/512 + py/256)/2,
    (py/256 - px/512)/2
  ];
}

function centerChunk(){
  const [dx,dy] = inverseChunk(state.cameraX,state.cameraY);
  return [state.startX + Math.round(dx), state.startY + Math.round(dy)];
}

function visibleRadius(){
  // v0.3: do not request an 11x11 neighborhood.
  // Use the configured prefetch radius only.
  // radius=1 -> 3x3 (9 chunks), radius=2 -> 5x5 (25 chunks).
  return Math.max(1, Math.min(3, Number(state.cfg?.prefetch_radius ?? 1)));
}

function wantedChunks(){
  const [cx,cy]=centerChunk();
  const r=visibleRadius();
  const arr=[];
  for(let dx=-r;dx<=r;dx++){
    for(let dy=-r;dy<=r;dy++){
      arr.push({x:cx+dx,y:cy+dy,d:Math.abs(dx)+Math.abs(dy)});
    }
  }
  arr.sort((a,b)=>a.d-b.d);
  return arr;
}

function needModes(){
  return state.mode==='overlay' ? ['static','save'] : [state.mode];
}

// Floor display policy:
//   B1 -> B1 only
//   1F -> 1F only
//   2F -> 1F + 2F
//   3F -> 1F + 2F + 3F
//
// z>=0 is returned bottom-to-top so higher floors are painted last.
function visibleLevels(){
  if(state.z < 0) return [state.z];
  const levels=[];
  for(let z=0; z<=state.z; z++) levels.push(z);
  return levels;
}

function enqueue(mode,x,y,z){
  const kk=key(x,y,z);
  const jobKey=`${mode}:${kk}`;
  const m=layers[mode];

  const existing=m.get(kk);
  if(existing && !existing.error) return;
  if(queued.has(jobKey) || loading.has(jobKey)) return;

  if(existing?.error) m.delete(kk);

  queued.add(jobKey);
  queue.push({mode,x,y,z,k:jobKey,gen:state.generation});
}

async function pump(){
  while(workers<MAX_WORKERS && queue.length){
    const job=queue.shift();
    queued.delete(job.k);
    loading.add(job.k);
    workers++;
    updateStats();

    loadChunk(job).finally(()=>{
      loading.delete(job.k);
      workers--;
      updateStats();
      refreshWanted(false);
      pump();
    });
  }
}

async function loadChunk(job){
  const map=layers[job.mode];
  const kk=key(job.x,job.y,job.z);
  try{
    const u=`/api/chunk?x=${job.x}&y=${job.y}&z=${job.z}&mode=${job.mode}`;
    const meta=await fetch(u).then(r=>{
      if(!r.ok) throw new Error(`${r.status}`);
      return r.json();
    });

    // A save switch increments generation. Never put an old-save response
    // into the new-save browser cache.
    if(job.gen!==state.generation) return;

    if(!meta.exists){
      map.set(kk,{meta,empty:true});
      draw();
      return;
    }

    const img=new Image();
    img.decoding='async';
    const ready=new Promise((resolve,reject)=>{
      img.onload=()=>resolve();
      img.onerror=()=>reject(new Error('image load'));
    });
    img.src=meta.image_url;
    await ready;
    if(job.gen!==state.generation){
      try{ img.src=''; }catch(_){}
      return;
    }
    map.set(kk,{meta,img,empty:false});
    draw();
  }catch(err){
    if(job.gen!==state.generation) return;
    map.set(kk,{error:String(err),empty:true});
  }
}

function pruneObsoleteQueue(wanted,modes){
  const allowed=new Set();
  const levels=visibleLevels();
  for(const mode of modes){
    for(const z of levels){
      for(const c of wanted){
        allowed.add(`${mode}:${key(c.x,c.y,z)}`);
      }
    }
  }

  let removed=0;
  for(let i=queue.length-1;i>=0;i--){
    const job=queue[i];
    if(!allowed.has(job.k)){
      queue.splice(i,1);
      queued.delete(job.k);
      removed++;
    }
  }
  return removed;
}

function reconcileWanted(wanted,modes){
  let added=0;
  const levels=visibleLevels();
  for(const mode of modes){
    const map=layers[mode];
    for(const z of levels){
      for(const c of wanted){
        const kk=key(c.x,c.y,z);
        const jobKey=`${mode}:${kk}`;
        const item=map.get(kk);
        const isLoaded=Boolean(item && !item.error);
        const isLoading=loading.has(jobKey);
        const isQueued=queued.has(jobKey);
        if(!isLoaded && !isLoading && !isQueued){
          enqueue(mode,c.x,c.y,z);
          added++;
        }
      }
    }
  }
  return added;
}

function pruneMemory(wanted,modes){
  // Keep only images that can currently be displayed.
  // This is especially important for cumulative 2F/3F rendering.
  const allowed=new Set();
  const levels=visibleLevels();

  for(const mode of modes){
    for(const z of levels){
      for(const c of wanted){
        allowed.add(`${mode}:${key(c.x,c.y,z)}`);
      }
    }
  }

  for(const mode of ['static','save']){
    const map=layers[mode];
    for(const [kk,item] of map){
      if(allowed.has(`${mode}:${kk}`)) continue;

      // Drop the Image reference so the browser can reclaim decoded RGBA.
      if(item && item.img){
        try{ item.img.src=''; }catch(_){}
      }
      map.delete(kk);
    }
  }
}

function refreshWanted(startPump=true){
  if(!state.cfg) return;

  const wanted=wantedChunks();
  const modes=needModes();
  pruneObsoleteQueue(wanted,modes);
  pruneMemory(wanted,modes);
  reconcileWanted(wanted,modes);

  if(startPump) pump();
  updateStats();
  draw();
}

function screenPos(wx,wy){
  return [
    wrap.clientWidth/2 + (wx-state.cameraX)*state.zoom,
    wrap.clientHeight/2 + (wy-state.cameraY)*state.zoom
  ];
}

function depthOrderedChunks(){
  // Stable world-space painter order. Never depend on camera distance.
  const arr=wantedChunks().slice();
  arr.sort((a,b)=>{
    const da=(a.x+a.y)-(b.x+b.y);
    if(da) return da;
    if(a.x!==b.x) return a.x-b.x;
    return a.y-b.y;
  });
  return arr;
}

// All floor PNGs use exactly the same world/chunk anchor.
// Do not apply a guessed extra per-floor offset here.
const Z_LEVEL_Y_OFFSET = 96*2;

function drawChunkLayer(mode,alpha,z){
  const map=layers[mode];
  const wanted=depthOrderedChunks();

  ctx.save();
  ctx.globalAlpha=alpha;
  ctx.imageSmoothingEnabled=false;

  for(const c of wanted){
    const item=map.get(key(c.x,c.y,z));
    if(!item || item.empty || item.error || !item.img) continue;

    const [bx,by]=chunkBase(c.x-state.startX,c.y-state.startY);

    // New fixed PNGs expose origin_x/origin_y.  Keep a compatibility
    // fallback for older variable-size cache entries.
    const fixed=Boolean(item.meta.fixed_size);
    const displayScale=fixed ? Number(item.meta.display_scale || 1) : 1;

    const xWorld=fixed
      ? bx - Number(item.meta.origin_x)*displayScale
      : bx + Number(item.meta.min_x);
    const yWorld=(fixed
      ? by - Number(item.meta.origin_y)*displayScale
      : by + Number(item.meta.min_y)) - z*Z_LEVEL_Y_OFFSET;

    const [sx,sy]=screenPos(xWorld,yWorld);
    const w=item.meta.width*displayScale*state.zoom;
    const h=item.meta.height*displayScale*state.zoom;
    ctx.drawImage(item.img,sx,sy,w,h);
  }
  ctx.restore();
}

function drawChunkGrid(){
  if(!state.showGrid) return;
  const wanted=depthOrderedChunks();

  ctx.save();
  ctx.lineWidth=1;
  ctx.strokeStyle='rgba(120,200,255,.35)';
  ctx.fillStyle='rgba(10,15,20,.72)';
  ctx.font='11px system-ui';
  ctx.textAlign='center';
  ctx.textBaseline='middle';

  for(const c of wanted){
    const [bx,by]=chunkBase(c.x-state.startX,c.y-state.startY);
    const pts=[
      [bx,by],
      [bx+512,by+256],
      [bx,by+512],
      [bx-512,by+256],
    ].map(([x,y])=>screenPos(x,y));

    ctx.beginPath();
    ctx.moveTo(pts[0][0],pts[0][1]);
    for(let i=1;i<pts.length;i++) ctx.lineTo(pts[i][0],pts[i][1]);
    ctx.closePath();
    ctx.stroke();

    const center=screenPos(bx,by+256);
    const text=`${c.x}/${c.y}`;
    const tw=ctx.measureText(text).width+10;
    ctx.fillRect(center[0]-tw/2,center[1]-8,tw,16);
    ctx.fillStyle='rgba(225,241,255,.92)';
    ctx.fillText(text,center[0],center[1]);
    ctx.fillStyle='rgba(10,15,20,.72)';
  }
  ctx.restore();
}

function draw(){
  const dpr=Math.max(1,window.devicePixelRatio||1);
  const w=wrap.clientWidth,h=wrap.clientHeight;
  ctx.save();
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);

  // Paint bottom-to-top.  For Overlay, paint static+save for each floor
  // before moving to the next floor so the higher floor stays visually above.
  for(const z of visibleLevels()){
    if(state.mode==='static') drawChunkLayer('static',1,z);
    else if(state.mode==='save') drawChunkLayer('save',1,z);
    else{
      drawChunkLayer('static',1,z);
      drawChunkLayer('save',state.opacity,z);
    }
  }
  drawChunkGrid();
  const playerZ=state.player ? Math.floor(Number(state.player.z)) : null;
  if(state.player && visibleLevels().includes(playerZ)){
    const px=state.player.x-state.startX*8;
    const py=state.player.y-state.startY*8;
    const [sx,sy]=screenPos((px-py)*64,(px+py)*32-playerZ*Z_LEVEL_Y_OFFSET);
    ctx.save();
    ctx.beginPath(); ctx.arc(sx,sy,8,0,Math.PI*2);
    ctx.fillStyle='rgba(255,255,255,.96)'; ctx.fill();
    ctx.lineWidth=3; ctx.strokeStyle='rgba(220,55,55,.95)'; ctx.stroke();
    ctx.beginPath(); ctx.moveTo(sx-13,sy); ctx.lineTo(sx+13,sy); ctx.moveTo(sx,sy-13); ctx.lineTo(sx,sy+13);
    ctx.lineWidth=1.5; ctx.strokeStyle='rgba(220,55,55,.95)'; ctx.stroke();
    ctx.restore();
  }
  ctx.restore();

  const [cx,cy]=centerChunk();
  hud.textContent=`chunk ${cx}/${cy}\nz=${state.z} [${visibleLevels().join(',')}]  ${state.mode}\nzoom ${Math.round(state.zoom*100)}%`;
  document.getElementById('centerChunk').textContent=`${cx}/${cy}`;
  document.getElementById('playerPos').textContent = state.player ? `${state.player.x.toFixed(2)}, ${state.player.y.toFixed(2)}, z=${Math.floor(state.player.z)}` : 'not found';
}

function updateStats(){
  let totalLoaded=0;
  for(const mode of ['static','save']){
    for(const x of layers[mode].values()){
      if(x && !x.error) totalLoaded++;
    }
  }

  let currentLoaded=0;
  if(state.cfg){
    const wanted=wantedChunks();
    for(const mode of needModes()){
      const map=layers[mode];
      for(const z of visibleLevels()){
        for(const c of wanted){
          const item=map.get(key(c.x,c.y,z));
          if(item && !item.error) currentLoaded++;
        }
      }
    }
  }

  document.getElementById('loadedCount').textContent=`${currentLoaded} / memory ${totalLoaded}`;
  document.getElementById('queueCount').textContent=`${queue.length}待機 / ${loading.size}読込中`;
  document.getElementById('status').textContent =
    loading.size || queue.length ? '現在必要なchunkだけ読み込み / レンダリング中…' : '準備完了';
}

function centerOnPlayer(){
  if(!state.player) return false;
  state.startX=state.player.chunk_x;
  state.startY=state.player.chunk_y;
  const lx=state.player.x-state.startX*8;
  const ly=state.player.y-state.startY*8;
  state.cameraX=(lx-ly)*64;
  state.cameraY=(lx+ly)*32;
  if(Number.isFinite(Number(state.player.z))) state.z=Math.floor(Number(state.player.z));
  return true;
}

function clearLayer(mode){
  const map=layers[mode];
  for(const item of map.values()){
    if(item && item.img){
      try{ item.img.src=''; }catch(_){}
    }
  }
  map.clear();
}

function setFloorButtons(){
  document.querySelectorAll('#floors button').forEach(b=>{
    b.classList.toggle('active',Number(b.dataset.z)===state.z);
  });
}

function saveLabel(item){
  if(!item) return '未選択';
  return item.label || item.name || item.path || 'save';
}

function renderSaveSelector(data){
  state.saveList=Array.isArray(data?.saves) ? data.saves : [];
  state.currentSave=data?.current || state.currentSave;

  const select=document.getElementById('saveSelect');
  const meta=document.getElementById('saveMeta');
  const version=document.getElementById('saveVersion');
  select.innerHTML='';

  version.textContent=`worldVersion ${data?.world_version ?? state.cfg?.save_world_version ?? '?'}`;

  if(!state.saveList.length){
    const opt=document.createElement('option');
    opt.value='';
    opt.textContent='対応するセーブがありません';
    select.appendChild(opt);
    select.disabled=true;
    meta.textContent=`検索先: ${data?.saves_root || state.cfg?.saves_root || '-'}`;
    return;
  }

  for(const item of state.saveList){
    const opt=document.createElement('option');
    opt.value=item.id;
    opt.textContent=saveLabel(item);
    if(state.currentSave && item.id===state.currentSave.id) opt.selected=true;
    select.appendChild(opt);
  }
  select.disabled=false;

  const current=state.currentSave;
  meta.textContent=current
    ? `${saveLabel(current)} / version ${current.world_version ?? '?'}`
    : `検索先: ${data?.saves_root || '-'}`;
}

async function loadSaveList(force=false){
  const url=force ? '/api/saves?refresh=1' : '/api/saves';
  const r=await fetch(url,{cache:'no-store'});
  if(!r.ok) throw new Error(`save list ${r.status}`);
  const data=await r.json();
  renderSaveSelector(data);
  return data;
}

async function switchSave(saveId){
  if(!saveId) return;

  const select=document.getElementById('saveSelect');
  const refreshBtn=document.getElementById('saveRefreshBtn');
  const meta=document.getElementById('saveMeta');
  select.disabled=true;
  refreshBtn.disabled=true;
  meta.textContent='セーブを切り替え中…';

  // Stop waiting jobs immediately. In-flight jobs are rejected by generation
  // checks in loadChunk() when they finish.
  clearGeneration();
  clearLayer('save');

  try{
    const r=await fetch('/api/select-save',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:saveId}),
      cache:'no-store',
    });
    const data=await r.json();
    if(!r.ok) throw new Error(data?.message || `HTTP ${r.status}`);

    state.currentSave=data.current || data.selected || null;
    state.player=data.player || null;

    if(state.player){
      centerOnPlayer();
    }else{
      state.startX=state.cfg.start_chunk_x;
      state.startY=state.cfg.start_chunk_y;
      state.z=state.cfg.start_z;
      state.cameraX=0;
      state.cameraY=0;
    }

    setFloorButtons();
    document.getElementById('info').textContent=
      'マップをクリックすると、そのworld squareを調べます。';

    await loadSaveList();
    refreshWanted();
  }catch(e){
    meta.textContent='切り替え失敗: '+e;
    await loadSaveList().catch(()=>{});
    throw e;
  }finally{
    select.disabled=state.saveList.length===0;
    refreshBtn.disabled=false;
  }
}

function resetCamera(){
  if(state.player) centerOnPlayer();
  else { state.cameraX=0; state.cameraY=0; }
  state.zoom=1;
  document.getElementById('zoom').value='100';
  document.getElementById('zoomText').textContent='100%';
  refreshWanted();
}

function clearGeneration(){
  state.generation++;
  queue.splice(0,queue.length);
  queued.clear();
}

function worldPixelAtScreen(clientX,clientY){
  const r=canvas.getBoundingClientRect();
  const sx=clientX-r.left;
  const sy=clientY-r.top;
  return [
    state.cameraX + (sx-r.width/2)/state.zoom,
    state.cameraY + (sy-r.height/2)/state.zoom,
  ];
}

async function inspectAt(clientX,clientY){
  const [wxpix,wypix]=worldPixelAtScreen(clientX,clientY);

  // inverse square isometric coordinates relative to start world's chunk origin
  const sx=(wxpix/64 + wypix/32)/2;
  const sy=(wypix/32 - wxpix/64)/2;

  const worldX=state.startX*8 + Math.floor(sx);
  const worldY=state.startY*8 + Math.floor(sy);

  const chunkX=Math.floor(worldX/8);
  const chunkY=Math.floor(worldY/8);
  const lx=worldX-chunkX*8;
  const ly=worldY-chunkY*8;

  const info=document.getElementById('info');
  info.textContent='読み込み中…';

  try{
    const u=`/api/square?x=${chunkX}&y=${chunkY}&lx=${lx}&ly=${ly}&z=${state.z}`;
    const d=await fetch(u).then(r=>r.json());

    const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    const st=d.static_tiles?.length
      ? '<ul>'+d.static_tiles.map(n=>`<li><code>${esc(n)}</code></li>`).join('')+'</ul>'
      : '<div class="small">なし</div>';
    const sv=d.save_objects?.length
      ? '<ul>'+d.save_objects.map(o=>`<li>${esc(o.class_name)}<br><code>${esc(o.sprite_name)}</code> <span class="small">#${o.sprite_id}</span></li>`).join('')+'</ul>'
      : '<div class="small">なし</div>';

    info.innerHTML=
      `<b>chunk</b> ${d.chunk_x}/${d.chunk_y}<br>`+
      `<b>local</b> (${d.local_x}, ${d.local_y}, z=${d.z})<br>`+
      `<b>world</b> (${d.world_x}, ${d.world_y}, z=${d.z})`+
      `<div style="margin-top:8px"><b>Static</b>${st}</div>`+
      `<div><b>Save main</b>${sv}</div>`;
  }catch(e){
    info.textContent='取得失敗: '+e;
  }
}

// pointer pan + click
let moved=false;
canvas.addEventListener('pointerdown',e=>{
  state.dragging=true;
  moved=false;
  state.dragX=e.clientX;
  state.dragY=e.clientY;
  canvas.classList.add('dragging');
  canvas.setPointerCapture(e.pointerId);
});
canvas.addEventListener('pointermove',e=>{
  if(!state.dragging) return;
  const dx=e.clientX-state.dragX;
  const dy=e.clientY-state.dragY;
  if(Math.abs(dx)+Math.abs(dy)>3) moved=true;
  state.cameraX -= dx/state.zoom;
  state.cameraY -= dy/state.zoom;
  state.dragX=e.clientX;
  state.dragY=e.clientY;
  draw();
});
canvas.addEventListener('pointerup',e=>{
  if(!state.dragging) return;
  state.dragging=false;
  canvas.classList.remove('dragging');
  if(!moved) inspectAt(e.clientX,e.clientY);
  refreshWanted();
});
canvas.addEventListener('pointercancel',()=>{
  state.dragging=false;
  canvas.classList.remove('dragging');
});

// wheel zoom centered on cursor
canvas.addEventListener('wheel',e=>{
  e.preventDefault();
  const before=worldPixelAtScreen(e.clientX,e.clientY);
  const factor=e.deltaY<0 ? 1.12 : 1/1.12;
  state.zoom=Math.max(.35,Math.min(2.2,state.zoom*factor));

  const r=canvas.getBoundingClientRect();
  const sx=e.clientX-r.left-r.width/2;
  const sy=e.clientY-r.top-r.height/2;
  state.cameraX=before[0]-sx/state.zoom;
  state.cameraY=before[1]-sy/state.zoom;

  const pct=Math.round(state.zoom*100);
  document.getElementById('zoom').value=String(pct);
  document.getElementById('zoomText').textContent=pct+'%';
  draw();
  refreshWanted();
},{passive:false});

document.querySelectorAll('#floors button').forEach(b=>{
  b.addEventListener('click',()=>{
    state.z=Number(b.dataset.z);
    clearGeneration();
    document.querySelectorAll('#floors button').forEach(x=>x.classList.toggle('active',x===b));
    refreshWanted();
  });
});
document.querySelectorAll('#modes button').forEach(b=>{
  b.addEventListener('click',()=>{
    state.mode=b.dataset.mode;
    clearGeneration();
    document.querySelectorAll('#modes button').forEach(x=>x.classList.toggle('active',x===b));
    refreshWanted();
  });
});
document.getElementById('opacity').addEventListener('input',e=>{
  state.opacity=Number(e.target.value)/100;
  document.getElementById('opacityText').textContent=e.target.value+'%';
  draw();
});
document.getElementById('zoom').addEventListener('input',e=>{
  state.zoom=Number(e.target.value)/100;
  document.getElementById('zoomText').textContent=e.target.value+'%';
  draw();
  refreshWanted();
});
document.getElementById('homeBtn').addEventListener('click',resetCamera);
document.getElementById('gridBtn').addEventListener('click',e=>{
  state.showGrid=!state.showGrid;
  e.currentTarget.classList.toggle('active',state.showGrid);
  draw();
});

document.getElementById('saveSelect').addEventListener('change',e=>{
  switchSave(e.target.value).catch(err=>{
    document.getElementById('status').textContent='セーブ切り替え失敗: '+err;
  });
});
document.getElementById('saveRefreshBtn').addEventListener('click',()=>{
  document.getElementById('saveMeta').textContent='セーブデータを再検索中…';
  loadSaveList(true).catch(err=>{
    document.getElementById('saveMeta').textContent='再検索失敗: '+err;
  });
});

window.addEventListener('resize',resize);

async function init(){
  state.cfg=await fetch('/api/config').then(r=>r.json());
  state.startX=state.cfg.start_chunk_x;
  state.startY=state.cfg.start_chunk_y;
  state.z=state.cfg.start_z;
  state.player=state.cfg.player || null;
  state.currentSave=state.cfg.current_save || null;
  if(state.player) centerOnPlayer();

  setFloorButtons();
  await loadSaveList();

  const staticBtn=document.querySelector('#modes button[data-mode="static"]');
  staticBtn.classList.add('active');

  resize();
  refreshWanted();
}

init().catch(e=>{
  document.getElementById('status').textContent='初期化失敗: '+e;
});
})();
