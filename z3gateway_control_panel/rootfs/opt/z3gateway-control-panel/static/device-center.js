/* Device-centered UI. Reuses the existing ingress API helper and bounded log buffer. */
(() => {
  const root = document.createElement('div'); root.id = 'device-center';
  document.getElementById('view-devices').prepend(root);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const hidden = new Set(['nodeId','targetNode','srcEui','srcEp','dstEp','otaEndpoint','otaManufacturerId','otaImageTypeId','otaFirmwareVersion','otaPayloadType']);
  let devices=[],commands=[],defaults={},selected=null,tab='概览',current=null,files=[],busy=false,allLogs=false,activeAction=null;
  const drafts = new Map();
  root.innerHTML = `<div class="dc-list panel"><div class="panel-title"><h2>设备</h2><button id="dc-reload">刷新</button></div><input id="dc-search" placeholder="搜索名称、型号、地址" aria-label="搜索设备"><div id="dc-cards"></div></div><div class="dc-main"><section class="panel dc-detail"><div id="dc-identity"></div><div id="dc-tabs" class="tabs"></div><div id="dc-content"></div></section><section class="panel dc-log-panel"><div class="panel-title"><h2>通信与操作日志</h2><div class="toolbar"><button id="dc-log-scope">显示全部日志</button><button id="dc-log-focus">专注日志</button></div></div><pre id="dc-log"></pre></section></div>`;
  const $d = id => document.getElementById(id);
  const request = (suffix, payload) => api('/api/device-center/'+encodeURIComponent(selected)+'/'+suffix,{method:'POST',body:JSON.stringify(payload||{})});
  function drawCards(){
    const term=$d('dc-search').value.toLowerCase();
    $d('dc-cards').innerHTML=devices.filter(d=>JSON.stringify(d).toLowerCase().includes(term)).map(d=>`<button class="dc-card ${d.id===selected?'active':''}" data-device="${escape(d.id)}"><strong>${escape(d.name||d.model||d.nodeId||'待确认设备')}</strong><span>${escape([d.manufacturer,d.model].filter(Boolean).join(' · ')||'资料待读取')}</span><span>${escape(d.deviceType||'类型未知')} · ${escape(d.networkRole||'角色未知')}</span><code>${escape(d.eui64||'IEEE 待确认')}</code><small>${escape(d.joined===false?'已离网':d.discovery||'pending')} · ${escape(d.lastSeen||'尚无通信')}</small></button>`).join('')||'<p class="muted">暂无设备，请在网关设置中启动网关并开放入网。</p>';
  }
  async function refresh(){
    if(busy||document.hidden||document.getElementById('view-devices').hidden) return;
    busy=true;
    try {
      const data=await api('/api/device-center');devices=data.devices;commands=data.commands;defaults=data.defaults;
      drawCards();
      if(selected&&!devices.some(d=>d.id===selected)){selected=null;current=null;drawDetail();}
      if(!selected){drawDetail();return;}
      const key=selected;const info=await api('/api/device-center/'+encodeURIComponent(key));
      if(key!==selected)return;
      const first=!current;
      const changed=first||JSON.stringify(current.device)!==JSON.stringify(info.device);
      const capabilities=first||JSON.stringify([current.device.endpoints,current.device.addressVerified])!==JSON.stringify([info.device.endpoints,info.device.addressVerified]);
      current=info;
      if(first)drawDetail();else drawIdentity();
      if(changed&&tab==='概览')drawBody();
      if(!first&&capabilities&&activeAction&&tab!=='概览')pickCommand(activeAction,false);
      drawLog();drawOperations();
    }catch(e){$d('dc-identity').textContent=e.message;}finally{busy=false;}
  }
  function drawIdentity(){
    if(!current)return;
    const d=current.device;
    $d('dc-identity').innerHTML=`<div class="panel-title"><div><h2>${escape(d.name||d.model||'设备详情')}</h2><p>${escape(d.eui64||'身份待确认')} · ${escape(d.nodeId||'地址待确认')} · ${escape(d.discovery||'pending')}</p></div><button id="dc-refresh-info">重新读取资料</button></div>`;
    $d('dc-refresh-info').onclick=async()=>{try{await request('refresh');toast('资料读取已排队');}catch(e){toast(e.message);}};
  }
  function drawDetail(){
    if(!selected||!current){$d('dc-identity').textContent='选择设备，查看资料并执行操作';$d('dc-tabs').innerHTML='';$d('dc-content').innerHTML='';$d('dc-log').textContent='';return;}
    drawIdentity();
    $d('dc-tabs').innerHTML=['概览','控制','属性','OTA','更多操作'].map(t=>`<button class="tab ${t===tab?'active':''}" data-section="${t}">${t}</button>`).join('');
    drawBody();drawLog();
  }
  function category(c){if(c.id==='ota_notify')return 'OTA';if(c.id.startsWith('read_')||c.id.startsWith('write_'))return '属性';if(c.id.startsWith('onoff_')||c.id==='level_move')return '控制';return '更多操作';}
  function drawBody(){
    const d=current.device,body=$d('dc-content');activeAction=null;
    if(tab==='概览'){
      const fields=[['厂商',d.manufacturer],['型号',d.model],['软件版本',d.swBuildId],['日期代码',d.dateCode],['功能类型',d.deviceType],['网络角色',d.networkRole],['最近通信',d.lastSeen],['离网请求',d.leaveRequest]];
      body.innerHTML=`<form id="dc-name-form" class="dc-name"><input id="dc-name" maxlength="80" aria-label="设备名称" placeholder="自定义设备名称" value="${escape(d.name||'')}"><button>保存名称</button></form><dl>${fields.map(([k,v])=>`<dt>${k}</dt><dd>${escape(v??'暂未获取')}</dd>`).join('')}</dl><h3>端点与能力</h3>${Object.entries(d.endpoints||{}).map(([ep,v])=>`<p>端点 ${escape(ep)} · ${escape(v.type)}<br><small>Profile ${escape(v.profile)} / Device ID ${escape(v.deviceId)}<br>服务端 Cluster：${v.inClusters.map(n=>'0x'+n.toString(16).padStart(4,'0')).join(', ')}</small></p>`).join('')||'<p>等待端点发现</p>'}<details><summary>属性读取状态</summary><pre>${escape(JSON.stringify(d.attributes,null,2))}</pre></details>`;
      $d('dc-name-form').onsubmit=async e=>{e.preventDefault();try{await request('rename',{name:$d('dc-name').value});await refresh();}catch(e){toast(e.message);}};
      return;
    }
    const options=commands.filter(c=>category(c)===tab);
    body.innerHTML=`<div class="dc-command-picker">${options.map(c=>`<button data-action="${escape(c.id)}">${escape(c.label)}</button>`).join('')}</div><div id="dc-form"></div><div id="dc-operations" aria-live="polite"></div>${tab==='更多操作'?'<button id="dc-delete" class="danger-text">强制删除设备</button><p class="muted">仅清理主机记录，不等待设备回复，不清理网络凭据。</p>':''}`;
    if($d('dc-delete'))$d('dc-delete').onclick=async()=>{if(!confirm(`强制删除 ${d.name||d.model||d.nodeId}\nIEEE: ${d.eui64||'待确认'}\n删除主机记录，不发送离网命令。`))return;try{await request('delete');drafts.delete(selected);selected=null;current=null;await refresh();toast('设备记录已删除');}catch(e){toast(e.message);}};
    drawOperations();
  }
  function pickCommand(id,autoExecute=true){
    activeAction=id;
    const c=commands.find(c=>c.id===id),d=current.device;
    const cached=drafts.get(selected+':'+id)||{};
    const fields=(c.fields||[]).filter(f=>!hidden.has(f.key));
    let endpoints=Object.entries(d.endpoints||{});
    const cluster=id.startsWith('onoff')?6:id==='level_move'?8:id==='ota_notify'?25:id.startsWith('read_')&&['read_manufacturer','read_model','read_date_code','read_sw_build'].includes(id)?0:null;
    if(cluster!==null)endpoints=endpoints.filter(([ep,v])=>(cluster===25?v.outClusters:v.inClusters).includes(cluster));
    const enabled=d.identity==='known'&&d.addressVerified&&d.nodeId&&(id==='zdo_leave'||endpoints.length);
    $d('dc-form').innerHTML=`<h3>${escape(c.label)}</h3>${id!=='zdo_leave'?`<label class="field"><span>设备端点</span><select name="endpoint">${endpoints.map(([ep,v])=>`<option value="${escape(ep)}" ${String(cached.endpoint)===ep?'selected':''}>端点 ${escape(ep)} · ${escape(v.type)}</option>`).join('')}</select></label>`:''}${id==='ota_notify'?`<label class="field"><span>OTA 镜像</span><select name="otaFile">${files.filter(f=>f.image).map(f=>`<option value="${escape(f.name)}">${escape(f.name)} · 版本 0x${f.image.firmwareVersion.toString(16)}</option>`).join('')}</select></label><p class="muted">镜像参数从文件头读取，设备是否接受升级以实际回复为准。</p>`:''}${fields.map(f=>`<label class="field"><span>${escape(f.label)}</span><input name="${escape(f.key)}" value="${escape(cached[f.key]??defaults[f.key]??'')}" autocomplete="off"></label>`).join('')}<details><summary>原始命令预览</summary><button type="button" id="dc-preview">生成预览</button><pre id="dc-preview-text"></pre></details><button id="dc-execute" class="primary" ${enabled?'':'disabled'}>执行：${escape(c.label)}</button>${enabled?'':'<p>等待设备身份、地址或支持的端点确认后才能执行。</p>'}`;
    const params=()=>Object.fromEntries([...$d('dc-form').querySelectorAll('[name]')].map(el=>[el.name,el.value]));
    $d('dc-form').oninput=()=>drafts.set(selected+':'+id,params());
    $d('dc-preview').onclick=async()=>{try{const r=await request('preview',{action:id,params:params()});$d('dc-preview-text').textContent=r.commands.join('\n');}catch(e){toast(e.message);}};
    const execute=async()=>{if(id==='zdo_leave'&&!confirm(`向 ${d.name||d.eui64} 发送离网请求？`))return;try{await request('operate',{action:id,params:params()});toast('操作已排队');await refresh();}catch(e){toast(e.message);}};
    $d('dc-execute').onclick=execute;
    if(autoExecute&&!fields.length&&id!=='ota_notify'&&id!=='zdo_leave'&&endpoints.length===1&&enabled)execute();
  }
  function drawOperations(){
    if(!$d('dc-operations')||!current)return;
    const labels={queued:'排队',sending:'发送中',waiting:'等待回复',success:'收到成功响应',failed:'失败',timeout:'回复超时','sent-unconfirmed':'已发送，执行结果未确认',cancelled:'已取消'};
    $d('dc-operations').innerHTML=(current.operations||[]).filter(j=>!j.auto).slice(-4).reverse().map(j=>`<div><strong>${escape(commands.find(c=>c.id===j.action)?.label||j.action)}：${labels[j.state]||escape(j.state)}</strong><pre>${escape(j.error||JSON.stringify(j.result??''))}</pre></div>`).join('');
  }
  function drawLog(){
    const el=$d('dc-log'),bottom=el.scrollTop+el.clientHeight>=el.scrollHeight-30;
    const text=allLogs?state.logText:(current?.events||[]).map(e=>`${e.at} ${e.event} ep:${e.ep} cluster:0x${e.cluster.toString(16)} ${e.data}`).join('\n')+'\n'+(current?.operations||[]).filter(j=>!j.auto).map(j=>`${j.created} ${j.action}: ${j.state} ${(j.commands||[]).join(' / ')} ${j.error||''}`).join('\n');
    if(el.textContent!==text){el.textContent=text;if(bottom)el.scrollTop=el.scrollHeight;}
  }
  root.onclick=async e=>{
    const card=e.target.closest('[data-device]');if(card){selected=card.dataset.device;current=null;tab='概览';await refresh();drawDetail();return;}
    const section=e.target.closest('[data-section]');if(section){tab=section.dataset.section;if(tab==='OTA'){try{files=(await api('/api/ota/files')).files;}catch(e){toast(e.message);}}drawDetail();return;}
    const action=e.target.closest('[data-action]');if(action)pickCommand(action.dataset.action);
  };
  $d('dc-search').oninput=drawCards;$d('dc-reload').onclick=refresh;
  $d('dc-log-scope').onclick=()=>{allLogs=!allLogs;$d('dc-log-scope').textContent=allLogs?'只看当前设备':'显示全部日志';drawLog();};
  $d('dc-log-focus').onclick=()=>root.classList.toggle('dc-focus');
  document.addEventListener('keydown',e=>{if(e.key==='Escape')root.classList.remove('dc-focus');});
  document.querySelectorAll('[data-view]').forEach(el=>el.addEventListener('click',()=>{root.classList.remove('dc-focus');refresh();}));
  document.getElementById('tab-console').textContent='高级控制台';document.getElementById('tab-devices').textContent='设备';document.getElementById('tab-settings').textContent='网关设置';
  document.getElementById('tab-devices').click();refresh();setInterval(refresh,2000);
})();
