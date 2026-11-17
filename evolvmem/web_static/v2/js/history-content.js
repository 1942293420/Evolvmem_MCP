/* Display-only transcript formatting. Original archived messages stay untouched. */
(function (root) {
  const E = root.EvolvMemV2, esc = E.esc;
  function decodeDisplay(value) {
    return String(value || '').replace(/&(?:#(x[0-9a-f]+|\d+)|amp|lt|gt|quot|apos|nbsp);/gi, (match, number) => {
      if (number) {
        const n = number[0].toLowerCase() === 'x' ? parseInt(number.slice(1),16) : Number(number);
        return n > 0 && n <= 0x10ffff && !(n >= 0xd800 && n <= 0xdfff) ? String.fromCodePoint(n) : match;
      }
      return {'&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"','&apos;':"'",'&nbsp;':' '}[match.toLowerCase()] || match;
    });
  }
  // Keep code fences literal; collapse only known injection syntax outside fences.
  function segments(text) {
    const lines=String(text || '').replace(/\r\n?/g,'\n').split('\n'), out=[];
    let plain=[];
    const flush=()=>{if(plain.length){out.push({kind:'text',text:plain.join('\n')});plain=[];}};
    for(let i=0;i<lines.length;i++) {
      const line=lines[i], fence=line.match(/^\s*(`{3,}|~{3,})([^`]*)$/);
      if(fence) {
        flush();const code=[], marker=fence[1][0], length=fence[1].length;
        while(++i<lines.length) {if(new RegExp('^\\s*'+marker+'{'+length+',}\\s*$').test(lines[i]))break;code.push(lines[i]);}
        out.push({kind:'code',text:code.join('\n'),label:fence[2].trim()});continue;
      }
      const tag=line.match(/^\s*<(skill|external_codex_[\w.-]+|environment_context|app-context)(?:\s[^>]*|)>/i);
      if(tag) {
        flush();const raw=[line], end=new RegExp('</'+tag[1]+'\\s*>','i');
        if(!end.test(line) && !/\/\s*>\s*$/.test(line))while(i+1<lines.length){raw.push(lines[++i]);if(end.test(lines[i]))break;}
        out.push({kind:'injection',label:tag[1].toLowerCase()==='skill'?'Skill 注入':tag[1],text:raw.join('\n')});continue;
      }
      if(/^#{1,6}\s+Files mentioned by the user\s*$/i.test(line)) {
        flush();const raw=[line];while(i+1<lines.length && lines[i+1].trim())raw.push(lines[++i]);
        out.push({kind:'injection',label:'用户附带的文件信息',text:raw.join('\n')});continue;
      }
      plain.push(line);
    }
    flush();return out;
  }
  E.historyDisplayTitle = function (value) {
    const text=segments(decodeDisplay(value)).filter(x=>x.kind==='text').map(x=>x.text).join(' ')
      .replace(/[\u200b-\u200f\ufeff]/g,'').replace(/^\s*#{1,6}\s*/,'').replace(/\s+/g,' ').trim();
    return text || '系统注入记录（展开原文查看）';
  };
  const inline = text => esc(decodeDisplay(text)).replace(/`([^`\n]+)`/g,'<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>');
  E.renderHistoryContent = function (value) {
    return segments(value).map(part=>{
      if(part.kind==='injection')return '<details class="history-injection"><summary>'+esc(part.label)+' · 展开原文</summary><pre>'+esc(part.text)+'</pre></details>';
      if(part.kind==='code')return '<pre class="history-code"><code>'+esc(part.text)+'</code></pre>';
      return part.text.split(/\n\s*\n/).filter(x=>x.trim()).map(block=>{
        const heading=block.match(/^#{1,6}\s+([^\n]+)$/);
        if(heading)return '<h3>'+inline(heading[1])+'</h3>';
        return '<div class="history-prose">'+inline(block)+'</div>';
      }).join('');
    }).join('');
  };
  E.historyMessages = function (block) {
    if(Array.isArray(block.messages))return block.messages;
    return (block.transcript || []).map((line,i)=>{
      const pos=line.indexOf('：'), label=pos>0?line.slice(0,pos):'消息';
      return {role:label==='助手'?'assistant':label==='用户'?'user':label,content:pos>0?line.slice(pos+1):line,recorded_at:(block.transcriptTimes || [])[i],time_basis:(block.transcriptTimeBases || [])[i]};
    });
  };
})(typeof window !== 'undefined' ? window : globalThis);
