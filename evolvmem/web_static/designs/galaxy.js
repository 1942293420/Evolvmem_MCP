/* Live project constellations. Node sizes reflect memory counts; edges share tags. */
(() => {
  'use strict';
  const instances=new WeakMap(), TAU=Math.PI*2;
  const random=n=>{const x=Math.sin(n*127.1+311.7)*43758.5453;return x-Math.floor(x);};
  const palettes={
    orbital:{ink:[44,79,66],accent:[44,127,95]},atlas:{ink:[101,89,65],accent:[113,128,79]},
    halo:{ink:[87,96,135],accent:[123,126,198]},signal:{ink:[66,80,109],accent:[44,86,210]},
    deep:{ink:[158,182,199],accent:[139,213,219]},
  };
  function mount(host,data) {
    const old=instances.get(host);if(old){old.setData(data);return;}
    const style=host.dataset.galaxy,palette=palettes[style]||palettes.orbital;
    const rgba=(which,a)=>`rgba(${palette[which].join(',')},${a})`;
    const canvas=document.createElement('canvas');canvas.className='galaxy-canvas';canvas.setAttribute('aria-label','项目记忆星图，使用下方项目选择器也可进入项目');
    const tip=document.createElement('div');tip.className='galaxy-tip';tip.hidden=true;
    const controls=document.createElement('div');controls.className='galaxy-controls';
    const hint=document.createElement('span');const pick=document.createElement('select');pick.setAttribute('aria-label','从星图选择项目');
    controls.append(hint,pick);host.append(canvas,tip,controls);
    const ctx=canvas.getContext('2d');
    let width=0,height=0,dpr=1,nodes=[],stars=[],edges=[],hover=null,visible=true;
    let lastTime=0,frameId=0,time=0,rotation=0;
    const reduced=matchMedia('(prefers-reduced-motion:reduce)');
    function resize(){const rect=host.getBoundingClientRect();if(rect.width<10||rect.height<10)return;width=rect.width;height=rect.height;dpr=Math.min(2,devicePixelRatio||1);canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);layout();draw();}
    function layout() {
      const centerX=width*.5,centerY=height*(style==='deep'?.48:.47);
      const radius=Math.min(width*.43,height*(style==='atlas'||style==='signal'?.40:.63));
      nodes.forEach((node,i)=>{
        let angle,rad;
        if(style==='atlas') {const ring=i<5?0:1;angle=(i<5?i/5:(i-5)/Math.max(1,nodes.length-5))*TAU-.8;rad=radius*(ring?.98:.59);}
        else if(style==='signal') {angle=i/Math.max(1,nodes.length)*TAU-.35;rad=radius*(i%3===0?.62:1);}
        else {const arm=i%3,level=Math.floor(i/3);angle=arm*TAU/3+.45+level*.57;rad=radius*(.44+.52*level/Math.max(1,Math.ceil(nodes.length/3)-1));}
        const a=angle+rotation;
        node.x=centerX+Math.cos(a)*rad;node.y=centerY+Math.sin(a)*rad*(style==='atlas'||style==='signal'?1:.68);
        node.radius=(style==='signal'?7:10)+Math.sqrt(node.items/(nodes[0]?.items||1))*14;
        node.orbit=node.radius*1.8+13;
      });
    }
    function setData(data) {
      const projects=data.projects.filter(p=>p.status==='active'&&p.active_items>0).sort((a,b)=>b.active_items-a.active_items);
      nodes=projects.slice(0,12).map((p,i)=>({slug:p.project,name:p.display_name||p.project,items:p.active_items,index:i,x:0,y:0,radius:10,tags:new Set()}));
      const map=new Map(nodes.map(n=>[n.slug,n]));
      stars=data.memories.map(m=>{
        const parent=map.get(m.project)||null;
        if(parent)(m.tags||'').split(',').map(t=>t.trim()).filter(t=>t.length>1&&!/^(分类:|project:)/.test(t)).forEach(t=>parent.tags.add(t));
        return {id:m.id,parent,phase:random(m.id*17)*TAU,distance:.55+random(m.id*31)*.9,size:1+Math.min(2,m.access_count/15),pinned:m.tier==='pinned'};
      });
      const pairs=[];
      for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){
        const shared=[...nodes[i].tags].filter(t=>nodes[j].tags.has(t)).length;
        if(shared>=2)pairs.push({a:nodes[i],b:nodes[j],weight:shared});
      }
      const degrees=new Map();edges=pairs.sort((a,b)=>b.weight-a.weight).filter(e=>{if((degrees.get(e.a)||0)>=3||(degrees.get(e.b)||0)>=3)return false;degrees.set(e.a,(degrees.get(e.a)||0)+1);degrees.set(e.b,(degrees.get(e.b)||0)+1);return true;});
      pick.replaceChildren(new Option('进入一个项目 ↗',''),...projects.map(p=>new Option(p.display_name||p.project,p.project)));
      hint.textContent=`${nodes.length} 个项目星系 · ${stars.length} 颗记忆星点`;
      hint.title='项目大小对应活跃记忆数量；星点展示按命中排序的前200条活跃记忆；项目连线来自共同标签。';
      hover=null;tip.hidden=true;layout();draw();
    }
    function glow(x,y,r,opacity) {const g=ctx.createRadialGradient(x,y,0,x,y,r);g.addColorStop(0,rgba('accent',opacity));g.addColorStop(.4,rgba('accent',opacity*.4));g.addColorStop(1,rgba('accent',0));ctx.fillStyle=g;ctx.fillRect(x-r,y-r,r*2,r*2);}
    function globe(x,y,r,angle,alpha=1) {
      ctx.lineWidth=.7;ctx.strokeStyle=rgba('ink',alpha*.55);ctx.beginPath();ctx.arc(x,y,r,0,TAU);ctx.stroke();
      const project=(px,py,pz)=>{const xx=px*Math.cos(angle)+pz*Math.sin(angle),zz=-px*Math.sin(angle)+pz*Math.cos(angle);return [x+xx,y+py*.93-zz*.32,zz];};
      for(let mer=0;mer<6;mer++) {
        const phi=mer*TAU/6;ctx.beginPath();
        for(let j=0;j<=48;j++){const t=j*Math.PI/48-Math.PI/2;const p=project(Math.cos(t)*Math.cos(phi)*r,Math.sin(t)*r,Math.cos(t)*Math.sin(phi)*r);if(j===0)ctx.moveTo(p[0],p[1]);else ctx.lineTo(p[0],p[1]);}
        ctx.strokeStyle=rgba('ink',alpha*.20);ctx.stroke();
      }
      for(const lat of [-.58,0,.58]) {
        ctx.beginPath();for(let j=0;j<=64;j++){const a=j*TAU/64,rr=Math.sqrt(1-lat*lat)*r,p=project(Math.cos(a)*rr,lat*r,Math.sin(a)*rr);if(j===0)ctx.moveTo(p[0],p[1]);else ctx.lineTo(p[0],p[1]);}ctx.strokeStyle=rgba('accent',alpha*.24);ctx.stroke();
      }
    }
    function background() {
      const x=width*.5,y=height*.47,r=Math.min(width*.40,height*.43);
      if(style==='signal') {
        ctx.strokeStyle=rgba('ink',.06);ctx.lineWidth=.6;
        for(let xx=20;xx<width;xx+=36){ctx.beginPath();ctx.moveTo(xx,20);ctx.lineTo(xx,height-50);ctx.stroke();}
        for(let yy=20;yy<height-50;yy+=36){ctx.beginPath();ctx.moveTo(20,yy);ctx.lineTo(width-20,yy);ctx.stroke();}
        ctx.strokeStyle=rgba('accent',.23);ctx.setLineDash([3,5]);ctx.beginPath();ctx.moveTo(20,y);ctx.lineTo(width-20,y);ctx.moveTo(x,15);ctx.lineTo(x,height-50);ctx.stroke();ctx.setLineDash([]);
      }
      if(style==='atlas'||style==='signal') {
        for(const scale of [.36,.68,1,1.12]){ctx.strokeStyle=rgba('ink',.12);ctx.lineWidth=.7;ctx.beginPath();ctx.arc(x,y,r*scale,0,TAU);ctx.stroke();}
        for(let i=0;i<72;i++){const a=i*TAU/72;ctx.strokeStyle=rgba('ink',i%6===0?.32:.12);ctx.beginPath();ctx.moveTo(x+Math.cos(a)*r*1.12,y+Math.sin(a)*r*1.12);ctx.lineTo(x+Math.cos(a)*(r*1.12+(i%6===0?8:3)),y+Math.sin(a)*(r*1.12+(i%6===0?8:3)));ctx.stroke();}
      } else {
        const radius=Math.min(width*.45,height*.62);
        [1,1.13,.70].forEach((scale,i)=>{ctx.strokeStyle=rgba('ink',style==='deep'?.11:.075);ctx.lineWidth=.7;ctx.beginPath();ctx.ellipse(x,y,radius*scale,radius*.66*scale,-.16+i*.06,0,TAU);ctx.stroke();});
        if(style==='deep') {glow(x,y,Math.min(width,height)*.5,.09);for(let i=0;i<95;i++){ctx.fillStyle=rgba('ink',.08+random(i+2)*.18);ctx.beginPath();ctx.arc(random(i*3+4)*width,random(i*7+1)*(height-50),.5+random(i)*.6,0,TAU);ctx.fill();}}
      }
    }
    function draw() {
      if(!width||!height)return;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,width,height);
      background();
      const cx=width*.5,cy=height*.47;
      edges.forEach((e,i)=>{
        const highlight=hover&&(e.a===hover||e.b===hover);ctx.strokeStyle=rgba('accent',highlight?.58:hover?.055:.17);ctx.lineWidth=highlight?1.15:.7;ctx.setLineDash(style==='signal'?[]:[3,5]);ctx.beginPath();ctx.moveTo(e.a.x,e.a.y);
        const mx=(e.a.x+e.b.x)/2+(e.b.y-e.a.y)*.12,my=(e.a.y+e.b.y)/2-(e.b.x-e.a.x)*.12;
        ctx.quadraticCurveTo(mx,my,e.b.x,e.b.y);ctx.stroke();ctx.setLineDash([]);
        const u=(time*.035+random(i*8))%1,v=1-u;ctx.fillStyle=rgba('accent',highlight?.9:.5);ctx.beginPath();ctx.arc(v*v*e.a.x+2*v*u*mx+u*u*e.b.x,v*v*e.a.y+2*v*u*my+u*u*e.b.y,1.7,0,TAU);ctx.fill();
      });
      stars.forEach(st=>{
        const n=st.parent,rad=n?n.orbit*st.distance:Math.min(width*.38,height*.48)*st.distance;
        const phase=st.phase+time*(n?.045:.015),x=(n?.x??cx)+Math.cos(phase)*rad,y=(n?.y??cy)+Math.sin(phase)*rad*.65;
        const opacity=hover&&n!==hover?.12:n?.7:.2;ctx.fillStyle=rgba(st.pinned?'accent':'ink',opacity);ctx.beginPath();ctx.arc(x,y,st.size*(width<500?.6:.8),0,TAU);ctx.fill();
      });
      const labelBoxes=[];
      nodes.forEach((node,i)=>{
        const opacity=hover&&hover!==node?.30:1;
        if(style==='atlas'||style==='signal') {
          ctx.fillStyle=rgba('accent',.08*opacity);ctx.strokeStyle=rgba('accent',.8*opacity);ctx.lineWidth=1;ctx.beginPath();ctx.arc(node.x,node.y,node.radius*.7,0,TAU);ctx.fill();ctx.stroke();
          ctx.fillStyle=rgba('accent',opacity);ctx.beginPath();ctx.arc(node.x,node.y,2.5,0,TAU);ctx.fill();
          if(style==='signal'){ctx.strokeStyle=rgba('accent',.3);ctx.beginPath();ctx.moveTo(node.x-8,node.y);ctx.lineTo(node.x+8,node.y);ctx.moveTo(node.x,node.y-8);ctx.lineTo(node.x,node.y+8);ctx.stroke();}
        }else{
          glow(node.x,node.y,node.orbit*1.5,(style==='deep'?.16:.11)*opacity);
          ctx.strokeStyle=rgba('ink',.13*opacity);ctx.lineWidth=.6;ctx.beginPath();ctx.ellipse(node.x,node.y,node.orbit,node.orbit*.55,-.3,0,TAU);ctx.stroke();
          globe(node.x,node.y,node.radius,time*.10+i*.63,opacity);
        }
        if(hover===node){ctx.strokeStyle=rgba('accent',.65);ctx.setLineDash([2,3]);ctx.beginPath();ctx.arc(node.x,node.y,node.radius+9,0,TAU);ctx.stroke();ctx.setLineDash([]);}
        const labelWidth=Math.min(135,node.name.length*11),ly=node.y+node.radius+12;
        const rect={x:node.x-labelWidth/2,y:ly,w:labelWidth,h:14};
        if(hover!==node&&labelBoxes.some(b=>Math.abs((b.x+b.w/2)-node.x)<(b.w+labelWidth)/2+5&&Math.abs(b.y-ly)<20))return;
        if(rect.x<5||rect.x+rect.w>width-5||ly>height-62)return;labelBoxes.push(rect);
        ctx.font=`${hover===node?'500':'400'} ${width<500?'10':'11'}px ${getComputedStyle(host).getPropertyValue('--body')||'sans-serif'}`;ctx.textAlign='center';ctx.textBaseline='top';ctx.fillStyle=rgba('ink',.85*opacity);ctx.fillText(node.name,node.x,ly,labelWidth);
      });
      glow(cx,cy,70,style==='deep'?.20:.1);
      if(style==='signal'||style==='atlas'){ctx.strokeStyle=rgba('accent',.4);ctx.beginPath();ctx.arc(cx,cy,25,0,TAU);ctx.stroke();ctx.beginPath();ctx.arc(cx,cy,19,0,TAU);ctx.stroke();}
      else globe(cx,cy,32,time*.07+.6,.85);
      ctx.textAlign='center';ctx.textBaseline='middle';ctx.font=`9px ${getComputedStyle(host).getPropertyValue('--mono')||'monospace'}`;ctx.fillStyle=rgba('ink',.66);ctx.fillText('EVOLVMEM',cx,cy+49);
    }
    function nearest(x,y) {return nodes.reduce((best,n)=>{const d=Math.hypot(n.x-x,n.y-y);return d<Math.max(22,n.radius+12)&&(!best||d<best.d)?{node:n,d}:best;},null)?.node||null;}
    function enter(node){if(node)document.dispatchEvent(new CustomEvent('evolvmem:project',{detail:node.slug}));}
    canvas.addEventListener('pointermove',event=>{
      const r=canvas.getBoundingClientRect(),x=event.clientX-r.left,y=event.clientY-r.top;hover=nearest(x,y);
      if(hover){tip.replaceChildren();const title=document.createElement('b'),body=document.createElement('span');title.textContent=hover.name;body.textContent=`${hover.items} 条活跃记忆 · 点击查看`;tip.append(title,body);tip.hidden=false;tip.style.left=Math.max(8,Math.min(width-260,x+18))+'px';tip.style.top=Math.max(8,Math.min(height-85,y-60))+'px';}else tip.hidden=true;draw();
    });
    canvas.addEventListener('pointerleave',()=>{hover=null;tip.hidden=true;draw();});
    canvas.addEventListener('click',event=>{const r=canvas.getBoundingClientRect();enter(nearest(event.clientX-r.left,event.clientY-r.top));});
    pick.onchange=()=>{const slug=pick.value;if(slug)document.dispatchEvent(new CustomEvent('evolvmem:project',{detail:slug}));pick.value='';};
    function frame(now){frameId=requestAnimationFrame(frame);if(!visible||document.hidden||reduced.matches||host.getBoundingClientRect().width<10)return;if(now-lastTime<34)return;lastTime=now;time=now/1000;rotation=style==='atlas'||style==='signal'?0:Math.sin(time*.012)*.045;layout();draw();}
    new ResizeObserver(resize).observe(host);
    new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;},{rootMargin:'100px'}).observe(host);
    window.addEventListener('resize',resize);reduced.addEventListener('change',draw);
    instances.set(host,{setData});setData(data);resize();frameId=requestAnimationFrame(frame);
  }
  window.EvolvGalaxy={mount};
})();
