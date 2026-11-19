const $ = (selector) => document.querySelector(selector);
const pages = {chat: $('#chatPage'), connections: $('#connectionsPage'), settings: $('#settingsPage')};
let chatHistory = [];
let toastTimer;
let localCallSequence = 0;
const localCalls = new Map();
window.onKurdLocalAiResult = (id, payload) => {
  const done = localCalls.get(id); if (!done) return;
  localCalls.delete(id);
  try { const result = JSON.parse(payload); result.ok ? done.resolve(result) : done.reject(new Error(result.error || 'Local inference failed')); }
  catch (error) { done.reject(error); }
};
function callLocalAi(method, value) {
  if (!window.KurdLocalAI || typeof window.KurdLocalAI[method] !== 'function') return Promise.reject(new Error('On-device AI is available in the Android app only.'));
  const id = `local_${++localCallSequence}`;
  return new Promise((resolve, reject) => {
    localCalls.set(id, {resolve, reject});
    try { window.KurdLocalAI[method](value, id); } catch (error) { localCalls.delete(id); reject(error); }
  });
}
const defaultSystem = 'You are Kurd Agent, a helpful assistant. Respond in the same language as the user. The user may write in Sorani Kurdish; respond naturally in Sorani Kurdish when appropriate. Be clear, accurate, and honest about uncertainty. When read-only GitHub tools are available and a question depends on the connected account or repositories, use those tools instead of guessing. Never claim to have changed or written repository data.';

function toast(message, error = false) {
  const box = $('#toast'); box.textContent = message; box.classList.toggle('error', error); box.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => box.classList.remove('show'), 3600);
}
function showPage(name) {
  Object.entries(pages).forEach(([key, node]) => node.classList.toggle('active', key === name));
  document.querySelectorAll('.nav-item').forEach(node => node.classList.toggle('active', node.dataset.page === name));
  $('#pageTitle').textContent = ({chat:'گفتوگۆ', connections:'پەیوەندییەکان', settings:'ڕێکخستن'})[name];
  if (name === 'connections') refreshGithub();
  if (name === 'settings') loadSettings();
}
document.querySelectorAll('.nav-item').forEach(node => node.addEventListener('click', () => showPage(node.dataset.page)));
$('#newChat').addEventListener('click', clearChat);
$('#mobileNewChat').addEventListener('click', clearChat);
function clearChat() { chatHistory = []; $('#messages').replaceChildren(); $('#welcome').hidden = false; $('#promptInput').focus(); }

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function addMessage(role, text, pending = false) {
  const row = document.createElement('div'); row.className = `message ${role}${pending ? ' pending' : ''}`;
  const avatar = role === 'assistant' ? '<div class="message-avatar">✳</div>' : '';
  row.innerHTML = `${avatar}<div class="message-body">${escapeHtml(text)}</div>`;
  $('#messages').append(row); row.scrollIntoView({behavior:'smooth', block:'nearest'}); return row;
}
async function sendPrompt(text) {
  const clean = text.trim(); if (!clean) return;
  $('#welcome').hidden = true; addMessage('user', clean); $('#promptInput').value = ''; autoGrow();
  chatHistory.push({role:'user', content:clean});
  const pending = addMessage('assistant', 'وەڵامەکە ئامادە دەکەم...', true); $('#sendButton').disabled = true;
  try {
    let result;
    if ($('#provider').value === 'android-local') {
      const stateResponse = await fetch('/api/local-ai/status'); const state = await stateResponse.json();
      if (!stateResponse.ok || !state.model_ready) throw new Error('یەکەم جار مۆدێلی GGUF لە ڕێکخستن دابەزێنە.');
      await callLocalAi('loadModel', state.model_path);
      const localHistory = chatHistory.slice(-5).map(message => ({...message, content:message.content.slice(-500)}));
      result = await callLocalAi('chat', JSON.stringify({messages:[{role:'system',content:defaultSystem}, ...localHistory], temperature:Number($('#temperature').value)}));
    } else {
      const response = await fetch('/api/chat', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({messages:[{role:'system', content:defaultSystem}, ...chatHistory]})});
      result = await response.json(); if (!response.ok) throw new Error(result.error || 'هەڵەیەک ڕوویدا');
    }
    pending.remove(); const reply = result.reply || 'وەڵامێک نەگەڕایەوە.'; chatHistory.push({role:'assistant', content:reply});
    const toolsNote = result.tools_used?.length ? '\n\n🧰 ئامرازی خوێندنەوەی GitHub بەکارهاتووە' : ''; addMessage('assistant', reply + toolsNote);
  } catch (error) {
    pending.remove(); chatHistory.pop(); addMessage('assistant', `نەتوانرا وەڵام بدرێتەوە. ${error.message}`); toast(error.message, true);
  } finally { $('#sendButton').disabled = false; $('#promptInput').focus(); }
}
$('#chatForm').addEventListener('submit', event => {event.preventDefault(); sendPrompt($('#promptInput').value);});
function autoGrow() { const field = $('#promptInput'); field.style.height = 'auto'; field.style.height = `${Math.min(field.scrollHeight,150)}px`; }
$('#promptInput').addEventListener('input', autoGrow);
$('#promptInput').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey) {event.preventDefault(); $('#chatForm').requestSubmit();} });
document.querySelectorAll('.suggestion').forEach(button => button.addEventListener('click', () => sendPrompt(button.dataset.prompt)));

document.querySelector('#themeButton').addEventListener('click', () => document.body.classList.toggle('light-mode'));
$('#temperature').addEventListener('input', () => $('#temperatureValue').textContent = Number($('#temperature').value).toFixed(1));
$('#provider').addEventListener('change', () => {if ($('#provider').value === 'ollama' && !$('#baseUrl').dataset.touched) $('#baseUrl').value = 'http://127.0.0.1:11434'; else if ($('#provider').value === 'openai-compatible' && !$('#baseUrl').dataset.touched) $('#baseUrl').value = 'http://127.0.0.1:1234/v1';});
$('#baseUrl').addEventListener('input', () => {$('#baseUrl').dataset.touched='true';});
async function loadSettings() {
  try {
    const response = await fetch('/api/status'); const data = await response.json();
    $('#provider').value = data.provider || 'ollama'; $('#baseUrl').value = data.base_url || ($('#provider').value === 'ollama' ? 'http://127.0.0.1:11434' : 'http://127.0.0.1:1234/v1');
    $('#baseUrl').dataset.touched = 'true'; $('#temperature').value = data.temperature ?? 0.7; $('#temperatureValue').textContent = Number($('#temperature').value).toFixed(1);
    $('#keySaved').hidden = !data.has_api_key; $('#clearApiKey').hidden = !data.has_api_key;
    $('#apiKey').dataset.clear = 'false';
    populateModels(data.models || [], data.model || '');
    await refreshLocalAiStatus();
  } catch { /* status check below will show service state */ }
}
function populateModels(models, selected = '') {
  const select = $('#modelSelect'); select.replaceChildren();
  const placeholder = document.createElement('option'); placeholder.value=''; placeholder.textContent = models.length ? 'مۆدێل هەڵبژێرە' : 'مۆدێل نەدۆزرایەوە — سێرڤەر بپشکنە'; select.append(placeholder);
  models.forEach(model => {const option = document.createElement('option'); option.value = model; option.textContent = model; select.append(option);});
  if (selected && !models.includes(selected)) {const opt=document.createElement('option'); opt.value=selected; opt.textContent=selected; select.append(opt);}
  select.value = selected;
}
function formatModelSize(bytes) {
  const size = Number(bytes || 0); if (!size) return 'قەبارە نادیار';
  return size >= 1024 ** 3 ? `${(size / 1024 ** 3).toFixed(2)} GB` : `${(size / 1024 ** 2).toFixed(0)} MB`;
}
async function refreshLocalAiStatus() {
  try {
    const response = await fetch('/api/local-ai/status'); const state = await response.json();
    $('#hfTokenSaved').hidden = !state.has_hf_token; $('#disconnectHf').hidden = !state.has_hf_token;
    $('#localHfRepo').value ||= state.repo || '';
    $('#localModelState').textContent = state.model_ready ? `ئامادەیە: ${state.filename}` : 'مۆدێل ئامادە نییە؛ GGUF دابەزێنە';
    if (state.model_ready && state.filename) {
      const select = $('#localHfFile');
      if (![...select.options].some(option => option.value === state.filename)) {
        const option = document.createElement('option'); option.value = state.filename; option.textContent = state.filename; select.append(option);
      }
      select.value = state.filename;
    }
    return state;
  } catch { return null; }
}
$('#connectHf').addEventListener('click', async () => {
  const token = $('#hfToken').value.trim(); if (!token) { toast('تۆکنی Hugging Face بنووسە', true); return; }
  const button = $('#connectHf'); button.disabled = true; button.textContent = 'پشکنین...';
  try {
    const response = await fetch('/api/huggingface/connect', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({token})});
    const data = await response.json(); if (!response.ok) throw new Error(data.error || 'پەیوەندی بە Hugging Face سەرکەوتوو نەبوو');
    $('#hfToken').value = ''; await refreshLocalAiStatus(); toast(`بە سەرکەوتوویی پەیوەست بوو: ${data.username}`);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = 'پەیوەستکردن'; }
});
$('#disconnectHf').addEventListener('click', async () => {
  try { const response = await fetch('/api/huggingface/connect', {method:'DELETE'}); if (!response.ok) throw new Error(); await refreshLocalAiStatus(); toast('تۆکنی Hugging Face سڕایەوە'); }
  catch { toast('سڕینەوەی تۆکن سەرکەوتوو نەبوو', true); }
});
$('#loadHfFiles').addEventListener('click', async () => {
  const repo = $('#localHfRepo').value.trim(); if (!repo) { toast('ناوی ڕێپۆ بنووسە', true); return; }
  const button = $('#loadHfFiles'); button.disabled = true; button.textContent = 'بار دەکرێت...';
  try {
    const response = await fetch('/api/local-ai/files?repo=' + encodeURIComponent(repo)); const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'فایلەکانی GGUF نەدۆزرایەوە');
    const select = $('#localHfFile'); select.replaceChildren();
    if (!data.files.length) { select.add(new Option('هیچ فایلی GGUF نییە', '')); throw new Error('ئەم ڕێپۆیە فایلی GGUF ـی گونجاوی نییە.'); }
    data.files.forEach(file => select.add(new Option(`${file.path} · ${formatModelSize(file.size)}`, file.path)));
    updateLocalFileSize(); toast(`${data.files.length} فایلی GGUF دۆزرایەوە`);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.innerHTML = 'فایلەکان <span>↻</span>'; }
});
function updateLocalFileSize() {
  const text = $('#localHfFile').selectedOptions[0]?.textContent || '';
  $('#localHfFileSize').textContent = text.includes(' · ') ? `— ${text.split(' · ').at(-1)}` : '';
}
$('#localHfFile').addEventListener('change', updateLocalFileSize);
$('#downloadLocalModel').addEventListener('click', async () => {
  const repo = $('#localHfRepo').value.trim(), filename = $('#localHfFile').value;
  if (!repo || !filename) { toast('ڕێپۆ و فایلی GGUF هەڵبژێرە', true); return; }
  const button = $('#downloadLocalModel'), progress = $('#localDownloadProgress'); button.disabled = true; button.textContent = 'دادەبەزێت...'; progress.hidden = false;
  $('#localDownloadStatus').textContent = 'پەیوەندی بە Hugging Face...'; $('#localDownloadPercent').textContent = '0%'; $('#localDownloadBar').style.width = '0%';
  try {
    const response = await fetch('/api/local-ai/download', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({repo, filename})});
    const data = await response.json(); if (!response.ok) throw new Error(data.error || 'داگرتنی مۆدێل دەستپێ نەکرا');
    let job;
    while (true) {
      await new Promise(resolve => setTimeout(resolve, 1200));
      const statusResponse = await fetch('/api/models/pull?id=' + encodeURIComponent(data.job_id)); const status = await statusResponse.json();
      if (!statusResponse.ok) throw new Error(status.error || 'دۆخی داگرتن بەردەست نییە');
      job = status.job; $('#localDownloadStatus').textContent = job.status || 'مۆدێل دادەبەزێت...';
      $('#localDownloadPercent').textContent = `${job.percent || 0}%`; $('#localDownloadBar').style.width = `${job.percent || 0}%`;
      if (job.state === 'error') throw new Error(job.error || 'داگرتنی مۆدێل سەرکەوتوو نەبوو');
      if (job.state === 'complete') break;
    }
    const state = await refreshLocalAiStatus();
    if (!state?.model_ready) throw new Error('فایلی GGUF لەسەر ئامێر پاشەکەوت نەکرا.');
    $('#provider').value = 'android-local';
    await saveModelSettings('');
    await callLocalAi('loadModel', state.model_path);
    $('#localDownloadStatus').textContent = 'مۆدێل لەسەر ئامێر لۆد کرا؛ inferencing ئۆفلاینە';
    await refreshStatus(); toast('مۆدێل لۆد کرا و بۆ چاتی ئۆفلاین ئامادەیە');
  } catch (error) { $('#localDownloadStatus').textContent = error.message; toast(error.message, true); }
  finally { button.disabled = false; button.innerHTML = 'داگرتن بۆ ئەم ئامێرە <span>↓</span>'; }
});
$('#refreshModels').addEventListener('click', async () => {
  const button = $('#refreshModels'); button.disabled=true; button.textContent='...';
  try {
    const cfg = {provider:$('#provider').value, base_url:$('#baseUrl').value || ($('#provider').value === 'ollama' ? 'http://127.0.0.1:11434':'http://127.0.0.1:1234/v1')};
    const save = await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...cfg,model:$('#modelSelect').value})});
    if (!save.ok) {const error=await save.json();throw new Error(error.error);}
    const response = await fetch('/api/models'); const data = await response.json(); if(!response.ok) throw new Error(data.error);
    populateModels(data.models || [], $('#modelSelect').value); toast(`${data.models.length} مۆدێل دۆزرایەوە`);
  } catch(error) {toast(error.message, true);} finally {button.disabled=false;button.textContent='↻ پشکنینی نوێ';}
});
$('#clearApiKey').addEventListener('click', () => {$('#apiKey').value='';$('#apiKey').dataset.clear='true';$('#keySaved').hidden=true;$('#clearApiKey').hidden=true;toast('دوای پاشەکەوت، API key دەسڕدرێتەوە');});
$('#saveSettings').addEventListener('click', async () => {
  const message=$('#settingsMessage'); message.textContent='پاشەکەوت دەکرێت...';
  try {
    const response=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:$('#provider').value,base_url:$('#baseUrl').value,model:$('#modelSelect').value,temperature:Number($('#temperature').value),api_key:$('#apiKey').value,clear_key:$('#apiKey').dataset.clear==='true'})});
    const data=await response.json();if(!response.ok)throw new Error(data.error);
    $('#apiKey').value='';$('#apiKey').dataset.clear='false';$('#keySaved').hidden=!data.has_api_key;$('#clearApiKey').hidden=!data.has_api_key;message.textContent='پاشەکەوت کرا ✓';$('#currentModel').textContent=data.settings.model||'مۆدێل هەڵنەبژێردراوە';
    await refreshStatus();setTimeout(()=>message.textContent='',2500);toast('ڕێکخستنەکان پاشەکەوت کران');
  }catch(error){message.textContent=error.message;toast(error.message,true);}
});

async function saveModelSettings(modelName = $('#modelSelect').value) {
  const response = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
    provider:$('#provider').value,
    base_url:$('#baseUrl').value,
    model:modelName,
    temperature:Number($('#temperature').value),
    api_key:$('#apiKey').value,
    clear_key:$('#apiKey').dataset.clear==='true'
  })});
  const data = await response.json(); if (!response.ok) throw new Error(data.error || 'ڕێکخستن پاشەکەوت نەکرا');
  $('#apiKey').value=''; $('#apiKey').dataset.clear='false'; $('#keySaved').hidden=!data.has_api_key; $('#clearApiKey').hidden=!data.has_api_key;
  return data;
}
$('#searchHf').addEventListener('click', async () => {
  const query=$('#hfSearch').value.trim(); if(query.length<2){toast('بۆ گەڕان لانیکەم دوو پیت بنووسە',true);return;}
  const button=$('#searchHf'), results=$('#hfResults'); button.disabled=true;button.textContent='دەگەڕێت...';results.replaceChildren();
  try {
    const response=await fetch('/api/huggingface/search?q='+encodeURIComponent(query));const data=await response.json();if(!response.ok)throw new Error(data.error);
    if(!data.models.length){results.textContent='هیچ ڕێپۆی GGUF نەدۆزرایەوە. دەتوانیت ناوی owner/model ـەکە بە دەست بنووسیت.';return;}
    data.models.forEach(model=>{
      const row=document.createElement('div');row.className='hf-result';
      const info=document.createElement('div');const name=document.createElement('div');name.className='hf-result-name';name.textContent=model.id;const meta=document.createElement('div');meta.className='hf-result-meta';meta.textContent=`${Number(model.downloads||0).toLocaleString('en-US')} downloads · ${Number(model.likes||0).toLocaleString('en-US')} likes`;info.append(name,meta);
      const choose=document.createElement('button');choose.className='text-button';choose.textContent='هەڵبژاردن';choose.addEventListener('click',()=>{$('#hfRepo').value=model.id;toast('ڕێپۆکە هەڵبژێردرا');});row.append(info,choose);results.append(row);
    });
  }catch(error){results.textContent=error.message;toast(error.message,true);}finally{button.disabled=false;button.innerHTML='گەڕان <span>⌕</span>';}
});
$('#hfSearch').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();$('#searchHf').click();}});
$('#downloadHf').addEventListener('click', async () => {
  const repo=$('#hfRepo').value.trim();if(!repo){toast('یەک ڕێپۆی Hugging Face هەڵبژێرە یان ناوی بنووسە',true);return;}
  if($('#provider').value!=='ollama'){toast('بۆ دابەزاندنی GGUF، سەرچاوەی Ollama هەڵبژێرە',true);return;}
  const button=$('#downloadHf'), progress=$('#downloadProgress');button.disabled=true;button.textContent='ئامادە دەکرێت...';progress.hidden=false;$('#downloadStatus').textContent='پەیوەندی بە Ollama...';$('#downloadPercent').textContent='0%';$('#downloadBar').style.width='0%';
  try {
    await saveModelSettings($('#modelSelect').value);
    const response=await fetch('/api/models/pull',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({repo,quant:$('#hfQuant').value})});const data=await response.json();if(!response.ok)throw new Error(data.error);
    let job;
    while(true){
      await new Promise(resolve=>setTimeout(resolve,1300));
      const result=await fetch('/api/models/pull?id='+encodeURIComponent(data.job_id));const status=await result.json();if(!result.ok)throw new Error(status.error||'دۆخی داگرتن بەردەست نییە');job=status.job;
      $('#downloadStatus').textContent=job.status||'مۆدێل دادەبەزێت...';$('#downloadPercent').textContent=`${job.percent||0}%`;
      $('#downloadBar').style.width=`${job.percent||0}%`;
      if(job.state==='error')throw new Error(job.error||'داگرتنی مۆدێل سەرکەوتوو نەبوو');
      if(job.state==='complete')break;
    }
    const modelsResponse=await fetch('/api/models');const modelsData=await modelsResponse.json();if(!modelsResponse.ok)throw new Error(modelsData.error);
    const modelName=modelsData.models.find(name=>name===job.model)||job.model;populateModels(modelsData.models||[],modelName);
    await saveModelSettings(modelName);$('#currentModel').textContent=modelName;await refreshStatus();
    $('#downloadStatus').textContent='مۆدێل ئامادەیە؛ دەتوانیت ئۆفلاین چات بکەیت';toast('مۆدێلەکە لە Ollama ئامادەیە');
  }catch(error){$('#downloadStatus').textContent=error.message;toast(error.message,true);}finally{button.disabled=false;button.innerHTML='دابەزاندن بۆ Ollama <span>↓</span>';}
});

async function refreshStatus() {
  const status=$('#modelStatus');
  try {
    const response=await fetch('/api/status'); const data=await response.json();
    status.classList.toggle('ok',data.available); status.classList.toggle('error',!data.available);
    status.lastElementChild.textContent=data.available ? (data.model || `${data.models.length} مۆدێل ئامادەیە`) : 'مۆدێل پەیوەست نییە';
    $('#currentModel').textContent=data.model||'مۆدێل هەڵنەبژێردراوە';
    $('#connectionCount').textContent=data.github_connected?'١':'٠';
    if(data.has_api_key) $('#keySaved').hidden=false;
  } catch {status.classList.add('error');status.lastElementChild.textContent='سێرڤەر بەردەست نییە';}
}

async function refreshGithub() {
  try {
    const response=await fetch('/api/github/state'); const data=await response.json();
    setGithubConnected(data.connected);
  } catch {setGithubConnected(false);}
}
function setGithubConnected(connected) {
  $('#githubBadge').textContent=connected?'پەیوەستە':'پەیوەست نییە'; $('#githubBadge').classList.toggle('connected-badge',connected);
  $('#githubConnectRow').hidden=connected; $('#githubConnected').hidden=!connected; $('#connectionCount').textContent=connected?'١':'٠';
}
$('#connectGithub').addEventListener('click', async () => {
  const token=$('#githubToken').value.trim(); if(!token){toast('تکایە GitHub token بنووسە',true);return;}
  const button=$('#connectGithub');button.disabled=true;button.textContent='پەیوەست دەبێت...';
  try {
    const response=await fetch('/api/github/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});const data=await response.json();if(!response.ok)throw new Error(data.error);
    $('#githubToken').value='';setGithubConnected(true);toast(`بە سەرکەوتوویی پەیوەست بوو: ${data.login}`);
  }catch(error){toast(error.message,true);}finally{button.disabled=false;button.innerHTML='پەیوەستکردن <span>↗</span>';}
});
$('#loadRepos').addEventListener('click', async () => {
  const list=$('#repoList');list.innerHTML='<span class="field-note">ڕێپۆکان بار دەکرێن...</span>';
  try {
    const response=await fetch('/api/github/repos');const data=await response.json();if(!response.ok)throw new Error(data.error);
    list.replaceChildren(); if(!data.repos.length){list.textContent='هیچ ڕێپۆیەک نەدۆزرایەوە.';return;}
    data.repos.forEach(repo=>{const a=document.createElement('a');a.className='repo-row';a.href=repo.url;a.target='_blank';a.rel='noopener noreferrer';a.innerHTML=`<span><strong>${escapeHtml(repo.name)}</strong><small>${escapeHtml(repo.description||'بێ وەسف')}</small></span><span class="repo-meta">${repo.private?'🔒 تایبەت':'☆ '+repo.stars}</span>`;list.append(a);});
  }catch(error){list.textContent=error.message;toast(error.message,true);}
});
$('#disconnectGithub').addEventListener('click', async () => {
  try {await fetch('/api/github/connect',{method:'DELETE'});setGithubConnected(false);$('#repoList').replaceChildren();toast('پەیوەندیی GitHub جیاکرایەوە');}
  catch{toast('جیاکردنەوە سەرکەوتوو نەبوو',true);}
});
$('#mcpInfo').addEventListener('click', () => {const panel=$('#mcpDetails');panel.hidden=!panel.hidden;});

// A lightweight shortcut for opening a fresh conversation.
document.addEventListener('keydown', event => {if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k'){event.preventDefault();clearChat();}});
refreshStatus();
