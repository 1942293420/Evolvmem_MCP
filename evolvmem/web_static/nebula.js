/* EvolvMem Neural Knowledge Nebula Engine v2 — Astrophysical Nebula & 3D Celestial Worlds
 * - Procedural multi-octave volumetric nebula cloud sprites & cosmic dust lanes
 * - Dynamically lit 3D Knowledge Planets (spherical terminator shading, atmospheric Rayleigh rim,
 *   front/back occluded planetary rings, and 3D-occluded orbiting evolution moons)
 * - Incandescent Skill Protostars / Pulsars with tilted accretion disks & tapered JWST diffraction rays
 * - Frameless astronomical star-chart leader callouts (zero bulky pill boxes!)
 */
(() => {
  "use strict";

  const esc = (v) =>
    String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }[c]));

  const CLUSTER_META = {
    evolvmem: {
      label: "系统与续接 · 核心星域",
      short: "SYSTEM",
      center: { x: -220, y: -38, z: 38 },
      abyssColor: [56, 189, 248],     // Ice Cyan
      deepTone: [12, 74, 138],
      crystalColor: [2, 132, 199],
      hexAbyss: "#7dd3fc",
      hexCrystal: "#0284c7"
    },
    "automation-control": {
      label: "前端与交互 · 体验星域",
      short: "FRONTEND",
      center: { x: 228, y: -44, z: -30 },
      abyssColor: [52, 211, 153],     // Aurora Jade
      deepTone: [6, 95, 70],
      crystalColor: [5, 150, 105],
      hexAbyss: "#6ee7b7",
      hexCrystal: "#059669"
    },
    "bluewhale-mcp": {
      label: "跨境与蓝鲸 · 业务星域",
      short: "BUSINESS",
      center: { x: 185, y: 105, z: 72 },
      abyssColor: [167, 139, 250],    // Violet Nebula
      deepTone: [76, 29, 149],
      crystalColor: [109, 40, 217],
      hexAbyss: "#c4b5fd",
      hexCrystal: "#7c3aed"
    },
    eva: {
      label: "飞书与协同 · 连续星域",
      short: "COLLAB",
      center: { x: -188, y: 102, z: -58 },
      abyssColor: [251, 191, 36],     // Warm Amber Giant
      deepTone: [146, 64, 14],
      crystalColor: [217, 119, 6],
      hexAbyss: "#fde68a",
      hexCrystal: "#d97706"
    },
    skill: {
      label: "Skill Hub · 恒星引力中枢",
      short: "PULSAR CORE",
      center: { x: 0, y: -6, z: 0 },
      abyssColor: [103, 232, 249],    // Pulsar White-Cyan
      deepTone: [14, 116, 144],
      crystalColor: [14, 116, 144],
      hexAbyss: "#ecfeff",
      hexCrystal: "#0891b2"
    }
  };

  const REL_META = {
    distill: {
      label: "Skill 升华沉淀",
      short: "升华",
      abyss: [103, 232, 249],
      crystal: [8, 145, 178],
      badgeCls: "badge-brand"
    },
    semantic: {
      label: "HNSW 语义近义",
      short: "HNSW",
      abyss: [96, 165, 250],
      crystal: [37, 99, 235],
      badgeCls: "badge-ok"
    },
    lineage: {
      label: "进化谱系派生",
      short: "演化",
      abyss: [192, 132, 252],
      crystal: [124, 58, 237],
      badgeCls: "badge-evolve"
    },
    bridge: {
      label: "跨项目迁移复用",
      short: "跨项",
      abyss: [52, 211, 153],
      crystal: [5, 150, 105],
      badgeCls: "badge-ok"
    }
  };

  function hashNum(str) {
    let h = 2166136261;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return Math.abs(h);
  }

  function clusterOf(project) {
    if (CLUSTER_META[project]) return CLUSTER_META[project];
    const h = hashNum(project || "other");
    const angle = ((h % 360) * Math.PI) / 180;
    return {
      label: `${project} · 扩展星域`,
      short: String(project || "EXT").toUpperCase().slice(0, 10),
      center: { x: Math.cos(angle) * 185, y: Math.sin(angle) * 90, z: (h % 100) - 50 },
      abyssColor: [56, 189, 248],
      deepTone: [12, 74, 138],
      crystalColor: [2, 132, 199],
      hexAbyss: "#7dd3fc",
      hexCrystal: "#0284c7"
    };
  }

  function rgba(rgb, a) {
    return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${Math.max(0, Math.min(1, a)).toFixed(3)})`;
  }

  function quadPoint(p0, p1, p2, t) {
    const inv = 1 - t;
    return {
      x: inv * inv * p0.x + 2 * inv * t * p1.x + t * t * p2.x,
      y: inv * inv * p0.y + 2 * inv * t * p1.y + t * t * p2.y
    };
  }

  // Persistent Nebula State
  const state = {
    canvas: null,
    ctx: null,
    rafId: 0,
    resizeObs: null,
    width: 860,
    height: 540,
    dpr: 1,

    lensMode: "abyss",     // "abyss" | "crystal"
    labelMode: "smart",    // "smart" (智能精简星图) | "hover" (纯净星云·仅焦点) | "all" (全部展开)
    autoRotate: true,
    relFilter: "all",
    projectFilter: "all",
    searchQuery: "",

    rotX: 0.24,
    rotY: -0.32,
    targetRotX: 0.24,
    targetRotY: -0.32,
    zoom: 1.0,
    targetZoom: 1.0,

    dragging: false,
    dragMoved: false,
    lastPtrX: 0,
    lastPtrY: 0,
    mouseX: -9999,
    mouseY: -9999,

    selectedId: "sk-1",
    hoveredId: null,

    nodes: [],
    links: [],
    nodeMap: new Map(),
    labelAlphas: new Map(),
    dust: [],
    nebulaClouds: [],
    cloudSprites: new Map(),
    burstPackets: [],
    shockwaves: [],

    callbacks: null,
    dbRef: null
  };

  // Pre-bake organic multi-puff volumetric nebula cloud sprite per color key (cached once)
  function getCloudSprite(cacheKey, rgbMain, rgbSecondary, isAbyss) {
    const fullKey = `${cacheKey}_${isAbyss ? "a" : "c"}`;
    if (state.cloudSprites.has(fullKey)) return state.cloudSprites.get(fullKey);
    if (typeof document === "undefined") return null;

    const size = 256;
    const off = document.createElement("canvas");
    off.width = size;
    off.height = size;
    const octx = off.getContext("2d");
    const half = size / 2;

    // Compose 26 organic overlapping off-axis gas puffs to form wispy filamentary nebula structure
    const seed = hashNum(fullKey);
    for (let i = 0; i < 26; i++) {
      const h = hashNum(`${fullKey}-${i}-${seed}`);
      const angle = ((h % 628) / 100);
      const dist = ((h >> 4) % 58) * (i < 6 ? 0.35 : 0.95);
      const px = half + Math.cos(angle) * dist;
      const py = half + Math.sin(angle) * dist * 0.68;
      const rad = 34 + ((h >> 8) % 56);
      const stretchX = 1.25 + ((h % 7) * 0.14);
      const stretchY = 0.48 + (((h >> 3) % 6) * 0.07);
      const rot = ((h >> 6) % 314) / 100;
      const rgb = i % 3 === 0 ? rgbSecondary : rgbMain;
      const alpha = isAbyss ? (i < 5 ? 0.052 : 0.028) : (i < 5 ? 0.055 : 0.030);

      octx.save();
      octx.translate(px, py);
      octx.rotate(rot);
      octx.scale(stretchX, stretchY);
      const g = octx.createRadialGradient(0, 0, 2, 0, 0, rad);
      g.addColorStop(0, rgba(rgb, alpha));
      g.addColorStop(0.45, rgba(rgb, alpha * 0.52));
      g.addColorStop(1, rgba(rgb, 0));
      octx.fillStyle = g;
      octx.beginPath();
      octx.arc(0, 0, rad, 0, Math.PI * 2);
      octx.fill();
      octx.restore();
    }

    state.cloudSprites.set(fullKey, off);
    return off;
  }

  // Initialize 260 deep-space stars & nebula dust motes + 16 volumetric nebula wisps
  (function initCosmicField() {
    const tints = [
      [224, 242, 254], // O/B ice-blue star
      [254, 249, 195], // G/F warm white-gold star
      [196, 181, 253], // Violet nebula star
      [167, 243, 208], // Mint star
      [255, 255, 255]  // Pure white field star
    ];
    for (let i = 0; i < 260; i++) {
      const h = hashNum(`star-${i}`);
      const theta = ((h % 3600) / 3600) * Math.PI * 2;
      const phi = Math.acos(2 * (((h >> 5) % 1000) / 1000) - 1);
      const r = 140 + ((h >> 9) % 360);
      const isBrightStar = i % 19 === 0;
      state.dust.push({
        x: r * Math.sin(phi) * Math.cos(theta),
        y: r * Math.sin(phi) * Math.sin(theta) * 0.64,
        z: r * Math.cos(phi),
        size: isBrightStar ? 1.8 + ((h % 10) / 10) : 0.45 + ((h % 13) / 12),
        bright: isBrightStar,
        phase: (h % 628) / 100,
        speed: 0.6 + ((h % 20) / 16),
        rgb: tints[i % tints.length]
      });
    }

    // 3D Nebula Cloud Wisps anchored along galactic arms & clusters
    const clusterList = ["evolvmem", "automation-control", "bluewhale-mcp", "eva", "skill"];
    clusterList.forEach((key, idx) => {
      const cm = CLUSTER_META[key];
      const count = key === "skill" ? 4 : 3;
      for (let k = 0; k < count; k++) {
        const h = hashNum(`wisp-${key}-${k}`);
        state.nebulaClouds.push({
          key,
          x: cm.center.x + ((h % 110) - 55) * (key === "skill" ? 0.9 : 0.75),
          y: cm.center.y + (((h >> 4) % 80) - 40) * 0.7,
          z: cm.center.z + (((h >> 8) % 90) - 45),
          scale: (key === "skill" ? 1.55 : 1.35) + (k * 0.18),
          rot: ((h % 314) / 100),
          rotSpeed: (k % 2 === 0 ? 1 : -1) * (0.025 + k * 0.008) + idx * 0.002
        });
      }
    });
  })();

  function buildGraph(db) {
    state.dbRef = db;
    const nodes = [];
    const nodeMap = new Map();
    const pendingByMem = new Map();
    (db.evolutionQueue || []).forEach((ev) => {
      if (ev.memoryId) pendingByMem.set(ev.memoryId, ev);
    });

    // 1. Central Skill Protostars / Pulsars
    const activeSkills = db.skills || [];
    activeSkills.forEach((sk, idx) => {
      const count = Math.max(1, activeSkills.length);
      const angle = (idx / count) * Math.PI * 2 - Math.PI / 2;
      const radius = count === 1 ? 0 : 74;
      const h = hashNum(sk.id);
      const node = {
        id: sk.id,
        kind: "skill",
        project: "skill",
        shortLabel: sk.shortLabel || sk.title.slice(0, 10),
        title: sk.title,
        subtitle: `${sk.slug} · ${sk.version}`,
        summary: sk.summary,
        confidence: 0.99,
        confirmedCount: (sk.syncedAgents || []).length * 2,
        status: "verified",
        scope: "public",
        hasPendingEvo: null,
        hasRing: true,
        ringTilt: -0.32 + (idx % 3) * 0.22,
        seed: h,
        raw: sk,
        baseX: Math.cos(angle) * radius,
        baseY: Math.sin(angle) * (radius * 0.54) - 6,
        baseZ: idx % 2 === 0 ? 24 : -24,
        phase: idx * 1.9,
        baseRadius: 11.5,
        degree: 0,
        projX: 0,
        projY: 0,
        projZ: 0,
        projScale: 1
      };
      nodes.push(node);
      nodeMap.set(node.id, node);
    });

    // 2. 3D Knowledge Planets grouped in Orbital Nebula Clusters
    const mems = (db.memories || []).filter((m) => m.status !== "archived");
    const byProj = new Map();
    mems.forEach((m) => {
      const list = byProj.get(m.project) || [];
      list.push(m);
      byProj.set(m.project, list);
    });

    byProj.forEach((list, proj) => {
      const cMeta = clusterOf(proj);
      list.forEach((m, idx) => {
        const golden = idx * 2.39996 + 0.4;
        const shellR = 52 + Math.sqrt(idx + 0.65) * 35;
        const h = hashNum(m.id);
        const zJitter = ((h % 96) - 48) * 0.95;
        const short =
          m.shortLabel ||
          (m.title.length > 12 ? `${m.title.slice(0, 11)}…` : m.title);
        const baseRadius =
          m.status === "active_low"
            ? 8.8
            : 11.5 + Math.min(6.2, (m.confirmedCount || 1) * 0.85);

        // Verified public worlds or multi-confirmed worlds earn Saturn-like planetary rings!
        const hasRing = m.scope === "public" || (m.confirmedCount || 0) >= 5;
        const ringTilt = (((h % 70) - 35) * Math.PI) / 180;

        const node = {
          id: m.id,
          kind: "memory",
          project: m.project,
          shortLabel: short,
          title: m.title,
          subtitle: `${m.project} · ${m.category} · ${m.version}`,
          summary: m.reusable || m.solution,
          confidence: m.confidence || 0.85,
          confirmedCount: m.confirmedCount || 0,
          status: m.status,
          scope: m.scope,
          hasPendingEvo: pendingByMem.get(m.id) || null,
          hasRing,
          ringTilt,
          moonCount: (pendingByMem.has(m.id) ? 1 : 0) + ((m.lineage?.length || 1) >= 2 ? 1 : 0),
          seed: h,
          raw: m,
          baseX: cMeta.center.x + Math.cos(golden) * shellR,
          baseY: cMeta.center.y + Math.sin(golden) * (shellR * 0.66),
          baseZ: cMeta.center.z + zJitter,
          phase: (h % 628) / 100,
          baseRadius,
          degree: 0,
          projX: 0,
          projY: 0,
          projZ: 0,
          projScale: 1
        };
        nodes.push(node);
        nodeMap.set(node.id, node);
      });
    });

    // 3. Knowledge Synaptic Streams
    const links = [];
    const seenPairs = new Set();
    const addLink = (src, tgt, relType, weight, label, reason) => {
      if (!nodeMap.has(src) || !nodeMap.has(tgt) || src === tgt) return;
      const key = src < tgt ? `${src}::${tgt}` : `${tgt}::${src}`;
      if (seenPairs.has(key)) return;
      seenPairs.add(key);
      const idx = links.length;
      links.push({
        id: `lnk-${idx}`,
        source: src,
        target: tgt,
        relType: REL_META[relType] ? relType : "semantic",
        weight: weight || 0.88,
        label: label || REL_META[relType]?.short || "关联",
        reason: reason || "基于共享项目上下文与向量语义建立星际引力关联。",
        curveSign: idx % 2 === 0 ? 1 : -1,
        pulseOffset: (idx * 0.23) % 1,
        pulseSpeed: 0.14 + (idx % 5) * 0.03
      });
      nodeMap.get(src).degree += 1;
      nodeMap.get(tgt).degree += 1;
    };

    (db.nebulaLinks || []).forEach((l) => {
      addLink(l.source, l.target, l.relType, l.weight, l.label, l.reason);
    });

    nodes.forEach((n) => {
      if (n.degree > 0) return;
      if (n.kind === "skill") {
        mems.slice(0, 2).forEach((m) => {
          addLink(m.id, n.id, "distill", 0.95, "新晋升沉淀", `由进化工坊裁决自动升华并合入共享恒星《${n.title}》。`);
        });
      } else {
        const peer = nodes.find((p) => p.id !== n.id && p.kind === "memory" && p.project === n.project);
        if (peer) addLink(n.id, peer.id, "lineage", 0.91, "同域演化", `同属 ${n.project} 星域，共享环境约束与演化脉络。`);
        if (activeSkills[0]) addLink(n.id, activeSkills[0].id, "distill", 0.88, "引力协同", `已纳入多智能体经验召回池。`);
      }
    });

    state.nodes = nodes;
    state.links = links;
    state.nodeMap = nodeMap;

    if (!state.selectedId || !nodeMap.has(state.selectedId)) {
      state.selectedId = nodes[0]?.id || null;
    }
  }

  function isNodeMatchingFilter(node) {
    if (state.projectFilter !== "all") {
      if (node.kind === "memory" && node.project !== state.projectFilter) return false;
      if (node.kind === "skill") {
        const hasProjLink = state.links.some((l) => {
          const otherId = l.source === node.id ? l.target : l.target === node.id ? l.source : null;
          const other = otherId ? state.nodeMap.get(otherId) : null;
          return other && other.project === state.projectFilter;
        });
        if (!hasProjLink) return false;
      }
    }
    if (state.searchQuery) {
      const q = state.searchQuery.toLowerCase();
      const hay = `${node.shortLabel} ${node.title} ${node.subtitle} ${node.summary}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }

  function isLinkActive(link) {
    if (state.relFilter !== "all" && link.relType !== state.relFilter) return false;
    const u = state.nodeMap.get(link.source);
    const v = state.nodeMap.get(link.target);
    if (!u || !v) return false;
    if (state.projectFilter !== "all") {
      if (u.project !== state.projectFilter && v.project !== state.projectFilter) return false;
    }
    return true;
  }

  function project3D(x, y, z, cx, cy) {
    const cosY = Math.cos(state.rotY);
    const sinY = Math.sin(state.rotY);
    const cosX = Math.cos(state.rotX);
    const sinX = Math.sin(state.rotX);

    const x1 = x * cosY - z * sinY;
    const z1 = z * cosY + x * sinY;
    const y2 = y * cosX - z1 * sinX;
    const z2 = z1 * cosX + y * sinX;

    const fov = 650;
    const scale = (fov / Math.max(190, fov + z2)) * state.zoom;
    return {
      x: cx + x1 * scale,
      y: cy + y2 * scale,
      z: z2,
      scale
    };
  }

  function focusNodeCamera(nodeId, emitPulse = true) {
    const node = state.nodeMap.get(nodeId);
    if (!node) return;
    state.selectedId = nodeId;

    const targetYaw = Math.atan2(node.baseX, -node.baseZ || 1) * 0.46;
    const targetPitch = Math.max(-0.52, Math.min(0.52, -node.baseY / 390));
    state.targetRotY = targetYaw;
    state.targetRotX = targetPitch;

    if (emitPulse) triggerStarResonance(nodeId);
    renderInspector();
  }

  function triggerStarResonance(centerId) {
    const id = centerId || state.selectedId;
    const node = state.nodeMap.get(id);
    if (!node) return;

    state.shockwaves.push({ nodeId: id, progress: 0, maxRadius: 92 });
    state.links.forEach((lnk) => {
      if (lnk.source === id || lnk.target === id) {
        const forward = lnk.source === id;
        state.burstPackets.push({
          link: lnk,
          t: forward ? 0 : 1,
          dir: forward ? 1 : -1,
          speed: 0.015 + Math.random() * 0.006
        });
      }
    });
  }

  function curveControlPoint(u, v, sign, cx, cy) {
    const mx = (u.projX + v.projX) * 0.5;
    const my = (u.projY + v.projY) * 0.5;
    const dx = v.projX - u.projX;
    const dy = v.projY - u.projY;
    const dist = Math.hypot(dx, dy) || 1;
    const bend = Math.min(44, dist * 0.18) * sign;
    const nx = (-dy / dist) * bend;
    const ny = (dx / dist) * bend;
    return {
      x: mx + nx + (cx - mx) * 0.07,
      y: my + ny + (cy - my) * 0.07
    };
  }

  // Draw tapered astronomical JWST-style diffraction ray (wide/bright at star, 0px & 0-alpha at tip)
  function drawTaperedRay(ctx, x, y, angle, length, halfThickness, colorStr) {
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(angle);
    const g = ctx.createLinearGradient(-length, 0, length, 0);
    g.addColorStop(0, "rgba(255,255,255,0)");
    g.addColorStop(0.38, colorStr);
    g.addColorStop(0.5, "rgba(255,255,255,0.95)");
    g.addColorStop(0.62, colorStr);
    g.addColorStop(1, "rgba(255,255,255,0)");
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.moveTo(-length, 0);
    ctx.quadraticCurveTo(0, -halfThickness, length, 0);
    ctx.quadraticCurveTo(0, halfThickness, -length, 0);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  // Render an incandescent Central Pulsar / Protostar (Skill Hub) — pure light & accretion disk, NO ball outline!
  function drawPulsarStar(ctx, n, x, y, r, cm, isAbyss, tSec, isFocus) {
    const rgb = isAbyss ? cm.abyssColor : cm.crystalColor;

    // 1. Wide inverse-square stellar bloom
    const outerR = r * (isFocus ? 5.2 : 4.1);
    const bloom = ctx.createRadialGradient(x, y, 0, x, y, outerR);
    if (isAbyss) {
      bloom.addColorStop(0, "rgba(255, 255, 255, 0.98)");
      bloom.addColorStop(0.10, "rgba(224, 247, 255, 0.84)");
      bloom.addColorStop(0.26, rgba(rgb, 0.34));
      bloom.addColorStop(0.58, rgba(rgb, 0.08));
      bloom.addColorStop(1, rgba(rgb, 0));
    } else {
      bloom.addColorStop(0, "rgba(255, 255, 255, 1)");
      bloom.addColorStop(0.14, rgba(rgb, 0.68));
      bloom.addColorStop(0.42, rgba(rgb, 0.22));
      bloom.addColorStop(1, rgba(rgb, 0));
    }
    ctx.fillStyle = bloom;
    ctx.beginPath();
    ctx.arc(x, y, outerR, 0, Math.PI * 2);
    ctx.fill();

    // 2. Tilted Glowing Accretion Disk (elliptical plasma ring around protostar)
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(n.ringTilt + Math.sin(tSec * 0.3 + n.phase) * 0.06);
    const diskRx = r * 2.55;
    const diskRy = r * 0.64;

    ctx.beginPath();
    ctx.ellipse(0, 0, diskRx, diskRy, 0, 0, Math.PI * 2);
    ctx.strokeStyle = rgba(rgb, isAbyss ? 0.55 : 0.48);
    ctx.lineWidth = 2.4;
    ctx.stroke();

    ctx.beginPath();
    ctx.ellipse(0, 0, diskRx * 1.24, diskRy * 1.24, 0, 0, Math.PI * 2);
    ctx.strokeStyle = rgba(rgb, isAbyss ? 0.22 : 0.20);
    ctx.lineWidth = 1.0;
    ctx.stroke();
    ctx.restore();

    // 3. Tapered JWST Diffraction Rays (4 primary + 4 subtle diagonal micro-rays)
    const rayLen = r * (isFocus ? 4.5 : 3.5) * (0.94 + 0.06 * Math.sin(tSec * 2.4 + n.phase));
    const rayColor = rgba(rgb, isAbyss ? 0.62 : 0.45);
    drawTaperedRay(ctx, x, y, 0, rayLen, 1.35, rayColor);
    drawTaperedRay(ctx, x, y, Math.PI / 2, rayLen * 0.88, 1.25, rayColor);
    drawTaperedRay(ctx, x, y, Math.PI / 4, rayLen * 0.45, 0.75, rgba(rgb, 0.32));
    drawTaperedRay(ctx, x, y, -Math.PI / 4, rayLen * 0.45, 0.75, rgba(rgb, 0.32));

    // 4. Pinpoint Incandescent Photosphere (seamless, zero border)
    const core = ctx.createRadialGradient(x, y, 0, x, y, r * 0.62);
    core.addColorStop(0, "#ffffff");
    core.addColorStop(0.65, isAbyss ? "#e0f7ff" : cm.hexCrystal);
    core.addColorStop(1, rgba(rgb, 0));
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(x, y, r * 0.62, 0, Math.PI * 2);
    ctx.fill();
  }

  // Render a true 3D Celestial World / Knowledge Planet lit from the central Pulsar nebula core!
  function drawPlanetWorld(ctx, n, x, y, r, cx, cy, cm, isAbyss, tSec) {
    const rgb = isAbyss ? cm.abyssColor : cm.crystalColor;
    const deep = cm.deepTone || [10, 30, 65];

    // Direction vector from this planet toward the central Nebula Pulsar light source
    let ldx = cx - x;
    let ldy = cy - y;
    const lDist = Math.hypot(ldx, ldy) || 1;
    ldx /= lDist;
    ldy /= lDist;
    // Slight upper-left bias if planet happens to cross near (cx, cy)
    if (lDist < 25) {
      ldx = -0.6;
      ldy = -0.8;
    }

    const ringRx = r * 2.35;
    const ringRy = r * 0.56;
    const tilt = n.ringTilt;

    // Helper for planetary ring half (backHalf = true draws behind sphere; false draws in front!)
    const drawRingHalf = (isFront) => {
      if (!n.hasRing) return;
      ctx.save();
      ctx.translate(x, y);
      ctx.rotate(tilt);
      const startA = isFront ? 0 : Math.PI;
      const endA = isFront ? Math.PI : Math.PI * 2;

      // Outer ring band
      ctx.beginPath();
      ctx.ellipse(0, 0, ringRx, ringRy, 0, startA, endA);
      ctx.strokeStyle = rgba(rgb, isAbyss ? (isFront ? 0.62 : 0.34) : (isFront ? 0.55 : 0.30));
      ctx.lineWidth = Math.max(1.4, r * 0.20);
      ctx.stroke();

      // Inner translucent Cassini ring band
      ctx.beginPath();
      ctx.ellipse(0, 0, ringRx * 0.77, ringRy * 0.77, 0, startA, endA);
      ctx.strokeStyle = isAbyss
        ? `rgba(224, 242, 254, ${isFront ? 0.38 : 0.18})`
        : rgba(rgb, isFront ? 0.32 : 0.16);
      ctx.lineWidth = Math.max(0.9, r * 0.10);
      ctx.stroke();
      ctx.restore();
    };

    // Orbiting 3D Moon helper (back or front pass for true occlusion!)
    const drawMoonsPass = (drawBehind) => {
      const totalMoons = n.moonCount || 0;
      if (totalMoons === 0) return;
      for (let mIdx = 0; mIdx < totalMoons; mIdx++) {
        const isEvoMoon = Boolean(n.hasPendingEvo) && mIdx === 0;
        const speed = isEvoMoon ? 1.85 : 1.05;
        const orbitAng = tSec * speed + n.phase + mIdx * 2.4;
        const behind = Math.sin(orbitAng) < 0;
        if (behind !== drawBehind) continue;

        const orbR = r * (isEvoMoon ? 1.85 : 1.55);
        const mxLocal = Math.cos(orbitAng) * orbR;
        const myLocal = Math.sin(orbitAng) * (orbR * 0.38);
        const cosT = Math.cos(tilt);
        const sinT = Math.sin(tilt);
        const mx = x + mxLocal * cosT - myLocal * sinT;
        const my = y + mxLocal * sinT + myLocal * cosT;
        const mr = Math.max(1.7, r * (isEvoMoon ? 0.25 : 0.18));

        // Subtle moon glow + tiny lit crescent moon
        const mRgb = isEvoMoon ? [251, 191, 36] : [186, 230, 253];
        const mg = ctx.createRadialGradient(mx, my, 0, mx, my, mr * 3.0);
        mg.addColorStop(0, isEvoMoon ? "#fef08a" : "#ffffff");
        mg.addColorStop(0.45, rgba(mRgb, 0.65));
        mg.addColorStop(1, rgba(mRgb, 0));
        ctx.fillStyle = mg;
        ctx.beginPath();
        ctx.arc(mx, my, mr * 3.0, 0, Math.PI * 2);
        ctx.fill();
      }
    };

    // 1. Back Moons & Back Half of Planetary Ring
    drawMoonsPass(true);
    drawRingHalf(false);

    // 2. Soft Rayleigh-Scattered Outer Atmosphere Glow (shifted slightly toward light source)
    const atmoR = r * 2.35;
    const atmo = ctx.createRadialGradient(
      x + ldx * r * 0.25,
      y + ldy * r * 0.25,
      r * 0.72,
      x,
      y,
      atmoR
    );
    atmo.addColorStop(0, rgba(rgb, isAbyss ? 0.42 : 0.28));
    atmo.addColorStop(0.45, rgba(rgb, isAbyss ? 0.14 : 0.10));
    atmo.addColorStop(1, rgba(rgb, 0));
    ctx.fillStyle = atmo;
    ctx.beginPath();
    ctx.arc(x, y, atmoR, 0, Math.PI * 2);
    ctx.fill();

    // 3. Clipped 3D Planet Sphere (Surface + Swirling Cloud Bands + Night-Side Terminator + Rim Scattering)
    ctx.save();
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.clip();

    // 3a. Sunlit Spherical Body Gradient
    const sunX = x + ldx * r * 0.52;
    const sunY = y + ldy * r * 0.52;
    const surf = ctx.createRadialGradient(sunX, sunY, r * 0.06, x - ldx * r * 0.25, y - ldy * r * 0.25, r * 1.35);
    if (isAbyss) {
      surf.addColorStop(0, "#e0f7ff");
      surf.addColorStop(0.28, cm.hexAbyss);
      surf.addColorStop(0.68, rgba(deep, 0.96));
      surf.addColorStop(1, "#030917");
    } else {
      surf.addColorStop(0, "#ffffff");
      surf.addColorStop(0.35, cm.hexAbyss);
      surf.addColorStop(0.78, cm.hexCrystal);
      surf.addColorStop(1, "#0f172a");
    }
    ctx.fillStyle = surf;
    ctx.fillRect(x - r - 2, y - r - 2, r * 2 + 4, r * 2 + 4);

    // 3b. Diverse Planetary Surface Biomes (Gas Storm Giant / Oceanic Aurora / Cratered Crust)
    const biome = n.seed % 3;
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(tilt * 0.7);
    const bandShift = Math.sin(tSec * 0.4 + n.phase) * (r * 0.14);
    if (biome === 0) {
      // Swirling Jovian / Neptunian storm bands + anticyclone oval
      [-0.46, -0.14, 0.18, 0.45].forEach((lat, bIdx) => {
        ctx.fillStyle =
          bIdx % 2 === 0
            ? `rgba(255, 255, 255, ${isAbyss ? 0.16 : 0.22})`
            : rgba(deep, isAbyss ? 0.36 : 0.20);
        ctx.beginPath();
        ctx.ellipse(bandShift * (bIdx % 2 === 0 ? 1 : -1), lat * r, r * 1.25, r * 0.13, 0, 0, Math.PI * 2);
        ctx.fill();
      });
      ctx.fillStyle = `rgba(255, 255, 255, ${isAbyss ? 0.24 : 0.30})`;
      ctx.beginPath();
      ctx.ellipse(bandShift * 0.8, r * 0.16, r * 0.26, r * 0.13, 0.1, 0, Math.PI * 2);
      ctx.fill();
    } else if (biome === 1) {
      // Archipelagic Nebula World + Polar Aurora Crown
      ctx.fillStyle = rgba(deep, isAbyss ? 0.38 : 0.22);
      ctx.beginPath();
      ctx.arc(-r * 0.22 + bandShift, -r * 0.12, r * 0.44, 0, Math.PI * 2);
      ctx.arc(r * 0.28 + bandShift * 0.6, r * 0.24, r * 0.36, 0, Math.PI * 2);
      ctx.fill();
      // Swirling white cloud wisps
      ctx.strokeStyle = `rgba(255, 255, 255, ${isAbyss ? 0.24 : 0.32})`;
      ctx.lineWidth = Math.max(1.1, r * 0.11);
      ctx.beginPath();
      ctx.arc(bandShift * 0.5, 0, r * 0.58, -0.8, 1.3);
      ctx.stroke();
    } else {
      // Mineral / Ice-Crust Exoplanet with subtle albedo basins
      ctx.fillStyle = `rgba(255, 255, 255, ${isAbyss ? 0.13 : 0.20})`;
      ctx.beginPath();
      ctx.ellipse(-r * 0.18, -r * 0.26, r * 0.38, r * 0.22, -0.3, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = rgba(deep, isAbyss ? 0.35 : 0.22);
      ctx.lineWidth = 1.0;
      ctx.beginPath();
      ctx.arc(r * 0.22, r * 0.18, r * 0.24, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.restore();

    // 3c. Deep Velvet Night-Side Terminator Shadow (real 3D crescent/gibbous lighting!)
    const shadowX = x + ldx * r * 0.68;
    const shadowY = y + ldy * r * 0.68;
    const termGrad = ctx.createRadialGradient(shadowX, shadowY, r * 0.40, shadowX, shadowY, r * 1.62);
    termGrad.addColorStop(0, "rgba(0, 0, 0, 0)");
    termGrad.addColorStop(0.54, isAbyss ? "rgba(3, 8, 20, 0.35)" : "rgba(15, 23, 42, 0.16)");
    termGrad.addColorStop(0.82, isAbyss ? "rgba(2, 6, 17, 0.86)" : "rgba(15, 23, 42, 0.52)");
    termGrad.addColorStop(1, isAbyss ? "rgba(1, 4, 12, 0.96)" : "rgba(15, 23, 42, 0.68)");
    ctx.fillStyle = termGrad;
    ctx.fillRect(x - r - 2, y - r - 2, r * 2 + 4, r * 2 + 4);

    // 3c-extra: Glowing Night-Side Tectonic Evolution Veins for actively evolving worlds!
    if (n.hasPendingEvo && isAbyss) {
      const nx = x - ldx * r * 0.42;
      const ny = y - ldy * r * 0.42;
      ctx.strokeStyle = `rgba(251, 191, 36, ${0.52 + 0.25 * Math.sin(tSec * 3 + n.phase)})`;
      ctx.lineWidth = 0.95;
      ctx.beginPath();
      ctx.moveTo(nx - r * 0.26, ny - r * 0.18);
      ctx.quadraticCurveTo(nx, ny + r * 0.05, nx + r * 0.22, ny + r * 0.26);
      ctx.moveTo(nx + r * 0.14, ny - r * 0.22);
      ctx.lineTo(nx - r * 0.06, ny + r * 0.12);
      ctx.stroke();
    }

    // 3d. Sunward Atmospheric Limb Scattering Crescent (NO full white outline!)
    const limbAngle = Math.atan2(ldy, ldx);
    ctx.beginPath();
    ctx.arc(x, y, r - 0.45, limbAngle - 1.25, limbAngle + 1.25);
    ctx.strokeStyle = isAbyss ? "rgba(224, 247, 255, 0.72)" : "rgba(255, 255, 255, 0.88)";
    ctx.lineWidth = 1.15;
    ctx.stroke();

    ctx.restore();

    // 4. Front Half of Planetary Ring & Front Moons (occluding the planet's equator!)
    drawRingHalf(true);
    drawMoonsPass(false);
  }

  // Subtle Broken-Arc Optical Rangefinder around focused/selected world (NOT a heavy solid circle!)
  function drawOpticalReticle(ctx, x, y, rad, rgb, isAbyss, tSec, isSelected) {
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(tSec * 0.28 * (isSelected ? 1 : -1));
    ctx.strokeStyle = isAbyss ? rgba(rgb, 0.85) : "#0284c7";
    ctx.lineWidth = isSelected ? 1.15 : 0.95;
    const span = 0.42;
    for (let k = 0; k < 4; k++) {
      const a0 = (k * Math.PI) / 2 + 0.18;
      ctx.beginPath();
      ctx.arc(0, 0, rad, a0, a0 + span);
      ctx.stroke();
    }
    ctx.restore();
  }

  function buildCalloutSlot(ctx, n, x, y, r, W, H, isFocus, goRight, goDown) {
    const clearR = n.hasRings || n.kind === "skill" ? r * 1.62 : r * 1.18;
    const diagDx = (isFocus ? 20 : 15) * goRight;
    const diagDy = (isFocus ? 18 : 14) * goDown;

    const startY = y + goDown * (r * 0.45);
    let elbowX = x + goRight * clearR + diagDx;
    let elbowY = Math.max(28, Math.min(H - 54, startY + diagDy));

    ctx.font = isFocus
      ? '600 13px -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif'
      : '500 12px -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif';
    const titleW = ctx.measureText(n.shortLabel).width;

    let dir = goRight;
    if (dir > 0 && elbowX + titleW + 16 > W - 12) {
      dir = -1;
      elbowX = x - clearR - 15;
    } else if (dir < 0 && elbowX - titleW - 16 < 12) {
      dir = 1;
      elbowX = x + clearR + 15;
    }

    const anchorX = x + dir * clearR;
    const shelfLen = titleW + 8;
    const endX = elbowX + dir * shelfLen;
    const textMidX = elbowX + dir * (shelfLen * 0.5);
    const hasSub = isFocus || n.kind === "skill";

    const rectL = Math.min(elbowX, endX) - 6;
    const rectR = Math.max(elbowX, endX) + 6;
    const rectT = elbowY - 19;
    const rectB = elbowY + (hasSub ? 17 : 5);

    return {
      dir,
      startY,
      anchorX,
      elbowX,
      elbowY,
      endX,
      textMidX,
      titleW,
      rect: { l: rectL, r: rectR, t: rectT, b: rectB }
    };
  }

  function rectsOverlap(a, b) {
    return a.l < b.r && a.r > b.l && a.t < b.b && a.b > b.t;
  }

  // Frameless Astronomical Star-Chart Callout (outward radial hairline leader + soft mist + crisp floating text, ZERO box!)
  function drawStarChartCallout(ctx, item, isAbyss) {
    const n = item.node;
    const alpha = item.alpha;
    if (alpha <= 0.03 || !item.slot) return;

    const rgb = item.rgb;
    const { dir, startY, anchorX, elbowX, elbowY, endX, textMidX, titleW } = item.slot;

    ctx.save();
    ctx.globalAlpha = alpha;

    ctx.font = item.isFocus
      ? '600 13px -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif'
      : '500 12px -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif';

    // 0. Invisible Feathered Cosmic Mist (absorbs bright nebula glare behind letters with ZERO border/box!)
    ctx.save();
    const mistRx = titleW * 0.70 + 20;
    const mistRy = item.isFocus || n.kind === "skill" ? 21 : 15;
    ctx.translate(textMidX, elbowY - 2);
    ctx.scale(1, mistRy / mistRx);
    const mist = ctx.createRadialGradient(0, 0, 2, 0, 0, mistRx);
    if (isAbyss) {
      mist.addColorStop(0, "rgba(2, 8, 20, 0.84)");
      mist.addColorStop(0.55, "rgba(2, 8, 20, 0.46)");
      mist.addColorStop(1, "rgba(2, 8, 20, 0)");
    } else {
      mist.addColorStop(0, "rgba(248, 251, 255, 0.88)");
      mist.addColorStop(0.6, "rgba(248, 251, 255, 0.48)");
      mist.addColorStop(1, "rgba(248, 251, 255, 0)");
    }
    ctx.fillStyle = mist;
    ctx.beginPath();
    ctx.arc(0, 0, mistRx, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();

    // 1. Hair-thin Optical Leader Line + Fading Gradient Horizon Shelf
    const lineGrad = ctx.createLinearGradient(anchorX, startY, endX, elbowY);
    lineGrad.addColorStop(0, rgba(rgb, item.isFocus ? 0.96 : 0.78));
    lineGrad.addColorStop(0.38, rgba(rgb, item.isFocus ? 0.82 : 0.54));
    lineGrad.addColorStop(1, rgba(rgb, 0.05));

    ctx.strokeStyle = lineGrad;
    ctx.lineWidth = item.isFocus ? 1.15 : 0.9;
    ctx.beginPath();
    ctx.moveTo(anchorX, startY);
    ctx.lineTo(elbowX, elbowY);
    ctx.lineTo(endX, elbowY);
    ctx.stroke();

    // Tiny luminous anchor dot at leader elbow
    ctx.fillStyle = isAbyss ? "#f0f9ff" : rgba(rgb, 0.95);
    ctx.beginPath();
    ctx.arc(elbowX, elbowY, item.isFocus ? 1.8 : 1.35, 0, Math.PI * 2);
    ctx.fill();

    // 2. Floating Frameless Title (crisp high-contrast star-chart typography, ZERO rectangle!)
    ctx.textAlign = dir > 0 ? "left" : "right";
    ctx.textBaseline = "bottom";
    const textX = elbowX + dir * 4;

    if (isAbyss) {
      ctx.shadowColor = "rgba(1, 5, 15, 0.98)";
      ctx.shadowBlur = 7;
      ctx.fillStyle = item.isFocus ? "#ffffff" : "rgba(248, 250, 252, 0.96)";
    } else {
      ctx.shadowColor = "rgba(255, 255, 255, 0.98)";
      ctx.shadowBlur = 6;
      ctx.fillStyle = item.isFocus ? "#0f172a" : "rgba(15, 23, 42, 0.92)";
    }
    ctx.fillText(n.shortLabel, textX, elbowY - 3);

    // 3. Subtle Micro-Telemetry Sub-caption ONLY on focused/pulsars
    if (item.isFocus || n.kind === "skill") {
      ctx.font = '600 10.5px "JetBrains Mono", Consolas, monospace';
      ctx.textBaseline = "top";
      const subMeta =
        n.kind === "skill"
          ? `PULSAR // ${n.raw.version}`
          : `${n.project.toUpperCase()} · ${Math.round(n.confidence * 100)}%`;
      ctx.fillStyle = isAbyss ? rgba(rgb, item.isFocus ? 0.96 : 0.86) : rgba(rgb, 0.92);
      ctx.fillText(subMeta, textX, elbowY + 3);
    }

    ctx.restore();
  }

  function drawFrame(ts) {
    if (!state.canvas || !state.ctx) return;
    const ctx = state.ctx;
    const W = state.width;
    const H = state.height;
    const cx = W * 0.5;
    const cy = H * 0.50;
    const tSec = (ts || 0) * 0.001;
    const isAbyss = state.lensMode === "abyss";

    if (state.autoRotate && !state.dragging && !state.hoveredId) {
      state.targetRotY += 0.0015;
    }
    state.rotY += (state.targetRotY - state.rotY) * 0.08;
    state.rotX += (state.targetRotX - state.rotX) * 0.08;
    state.zoom += (state.targetZoom - state.zoom) * 0.12;

    ctx.save();
    ctx.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    // ==================== LAYER 1: DEEP COSMIC BACKDROP ====================
    const bgGrad = ctx.createRadialGradient(cx, cy * 0.94, 10, cx, cy, Math.max(W, H) * 0.82);
    if (isAbyss) {
      bgGrad.addColorStop(0, "#0b1f3d");
      bgGrad.addColorStop(0.45, "#051124");
      bgGrad.addColorStop(1, "#020712");
    } else {
      bgGrad.addColorStop(0, "#f8fbff");
      bgGrad.addColorStop(0.52, "#e5f0fc");
      bgGrad.addColorStop(1, "#d5e4f6");
    }
    ctx.fillStyle = bgGrad;
    ctx.fillRect(0, 0, W, H);

    // Whisper-thin Celestial Equatorial Astrolabe Ellipses
    ctx.save();
    ctx.translate(cx, cy);
    const tiltRatio = Math.max(0.32, Math.cos(state.rotX) * 0.56);
    [150, 265].forEach((r, idx) => {
      ctx.beginPath();
      ctx.ellipse(0, 0, r * state.zoom, r * tiltRatio * state.zoom, state.rotY * 0.12, 0, Math.PI * 2);
      ctx.strokeStyle = isAbyss
        ? `rgba(125, 211, 252, ${0.075 - idx * 0.02})`
        : `rgba(2, 132, 199, ${0.11 - idx * 0.03})`;
      ctx.lineWidth = 0.8;
      if (idx === 1) ctx.setLineDash([3, 8]);
      ctx.stroke();
    });
    ctx.setLineDash([]);
    ctx.restore();

    // ==================== LAYER 2: MULTI-OCTAVE VOLUMETRIC NEBULA WISPS ====================
    ctx.save();
    if (isAbyss) ctx.globalCompositeOperation = "screen";

    state.nebulaClouds.forEach((wisp) => {
      if (state.projectFilter !== "all" && wisp.key !== "skill" && wisp.key !== state.projectFilter) return;
      const cm = CLUSTER_META[wisp.key];
      const secondary = wisp.key === "evolvmem" ? [99, 102, 241] : [56, 189, 248];
      const sprite = getCloudSprite(wisp.key, isAbyss ? cm.abyssColor : cm.crystalColor, secondary, isAbyss);
      if (!sprite) return;

      const p = project3D(wisp.x, wisp.y, wisp.z, cx, cy);
      const drawSize = 270 * wisp.scale * p.scale;
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(wisp.rot + tSec * wisp.rotSpeed);
      ctx.globalAlpha = isAbyss ? (wisp.key === "skill" ? 0.46 : 0.74) : 0.72;
      ctx.drawImage(sprite, -drawSize / 2, -drawSize / 2, drawSize, drawSize);
      ctx.restore();
    });
    ctx.restore();

    // Subtle Sector Nebula Coordinates (ultra-clean astronomical watermark pushed clear of planets)
    ["evolvmem", "automation-control", "bluewhale-mcp", "eva"].forEach((key) => {
      if (state.projectFilter !== "all" && key !== state.projectFilter) return;
      const cm = CLUSTER_META[key];
      const p = project3D(cm.center.x * 1.12, cm.center.y * 1.25, cm.center.z, cx, cy);
      const rgb = isAbyss ? cm.abyssColor : cm.crystalColor;
      const yOff = (cm.center.y < 0 ? -78 : 88) * p.scale;
      ctx.font = '500 10.5px "JetBrains Mono", Consolas, monospace';
      ctx.textAlign = "center";
      ctx.fillStyle = rgba(rgb, isAbyss ? 0.24 : 0.34);
      ctx.fillText(`· ${cm.short} SECTOR ·`, p.x, Math.max(22, Math.min(H - 54, p.y + yOff)));
    });

    // ==================== LAYER 3: DEEP-SPACE STARFIELD & BRIGHT FIELD STARS ====================
    ctx.save();
    state.dust.forEach((d) => {
      const p = project3D(d.x, d.y, d.z, cx, cy);
      if (p.x < -10 || p.x > W + 10 || p.y < -10 || p.y > H + 10) return;
      const twinkle = 0.35 + 0.65 * (0.5 + 0.5 * Math.sin(tSec * d.speed + d.phase));
      const r = Math.max(0.45, d.size * p.scale * 0.85);
      const rgb = isAbyss ? d.rgb : [30, 64, 175];
      const alpha = isAbyss ? twinkle * (d.bright ? 0.85 : 0.52) : twinkle * 0.28;

      ctx.fillStyle = rgba(rgb, alpha);
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.fill();

      // Tiny cross glint on occasional bright background stars
      if (d.bright && isAbyss && twinkle > 0.72) {
        ctx.strokeStyle = rgba(rgb, alpha * 0.45);
        ctx.lineWidth = 0.6;
        const gl = r * 3.4;
        ctx.beginPath();
        ctx.moveTo(p.x - gl, p.y);
        ctx.lineTo(p.x + gl, p.y);
        ctx.moveTo(p.x, p.y - gl);
        ctx.lineTo(p.x, p.y + gl);
        ctx.stroke();
      }
    });
    ctx.restore();

    // Project 3D Celestial Worlds
    state.nodes.forEach((n) => {
      const floatX = Math.cos(tSec * 0.55 + n.phase) * 3.2;
      const floatY = Math.sin(tSec * 0.7 + n.phase * 1.3) * 2.8;
      const floatZ = Math.sin(tSec * 0.48 + n.phase) * 3.6;
      const p = project3D(n.baseX + floatX, n.baseY + floatY, n.baseZ + floatZ, cx, cy);
      n.projX = p.x;
      n.projY = p.y;
      n.projZ = p.z;
      n.projScale = p.scale;
      n.matchesFilter = isNodeMatchingFilter(n);
    });

    // Screen-space hover hit test
    const sortedFrontFirst = [...state.nodes].sort((a, b) => a.projZ - b.projZ);
    let nextHover = null;
    if (!state.dragging && state.mouseX >= 0) {
      for (const n of sortedFrontFirst) {
        const hitR = n.baseRadius * n.projScale * 1.45 + 9;
        if (Math.hypot(state.mouseX - n.projX, state.mouseY - n.projY) <= hitR) {
          nextHover = n.id;
          break;
        }
      }
    }
    if (nextHover !== state.hoveredId) {
      state.hoveredId = nextHover;
      state.canvas.style.cursor = nextHover ? "pointer" : state.dragging ? "grabbing" : "grab";
    }

    const hoverOnly = state.hoveredId;
    const focusId = hoverOnly || state.selectedId;
    const connectedIds = new Set();
    const highlightedLinks = new Set();
    if (focusId) {
      connectedIds.add(focusId);
      state.links.forEach((l) => {
        if (!isLinkActive(l)) return;
        if (l.source === focusId || l.target === focusId) {
          connectedIds.add(l.source);
          connectedIds.add(l.target);
          highlightedLinks.add(l.id);
        }
      });
    }

    // ==================== LAYER 4: GOSSAMER STARDUST ENERGY STREAMS ====================
    const sortedLinks = [...state.links].sort((a, b) => {
      const za = (state.nodeMap.get(a.source)?.projZ || 0) + (state.nodeMap.get(a.target)?.projZ || 0);
      const zb = (state.nodeMap.get(b.source)?.projZ || 0) + (state.nodeMap.get(b.target)?.projZ || 0);
      return zb - za;
    });

    sortedLinks.forEach((l) => {
      if (!isLinkActive(l)) return;
      const u = state.nodeMap.get(l.source);
      const v = state.nodeMap.get(l.target);
      if (!u || !v) return;

      const isHi = highlightedLinks.has(l.id);
      const rMeta = REL_META[l.relType] || REL_META.semantic;
      const rgb = isAbyss ? rMeta.abyss : rMeta.crystal;
      const ctrl = curveControlPoint(u, v, l.curveSign, cx, cy);

      // Tapered gradient filament fading gently near planet atmospheres
      const grad = ctx.createLinearGradient(u.projX, u.projY, v.projX, v.projY);
      const midA = isHi
        ? (isAbyss ? 0.72 : 0.68)
        : hoverOnly
        ? 0.08
        : (isAbyss ? 0.24 : 0.22);
      const endA = midA * 0.35;
      grad.addColorStop(0, rgba(rgb, endA));
      grad.addColorStop(0.5, rgba(rgb, midA));
      grad.addColorStop(1, rgba(rgb, endA));

      if (isHi) {
        ctx.beginPath();
        ctx.moveTo(u.projX, u.projY);
        ctx.quadraticCurveTo(ctrl.x, ctrl.y, v.projX, v.projY);
        ctx.strokeStyle = rgba(rgb, isAbyss ? 0.16 : 0.12);
        ctx.lineWidth = 4.2;
        ctx.stroke();
      }

      ctx.beginPath();
      ctx.moveTo(u.projX, u.projY);
      ctx.quadraticCurveTo(ctrl.x, ctrl.y, v.projX, v.projY);
      ctx.strokeStyle = grad;
      ctx.lineWidth = isHi ? 1.45 : 0.85;
      ctx.stroke();

      // Ethereal stardust motes drifting along active streams
      if (isHi || !hoverOnly) {
        const ptT = (tSec * l.pulseSpeed + l.pulseOffset) % 1;
        for (let seg = 0; seg < 4; seg++) {
          const tailT = Math.max(0, ptT - seg * 0.02);
          const pos = quadPoint({ x: u.projX, y: u.projY }, ctrl, { x: v.projX, y: v.projY }, tailT);
          const pAlpha = (isHi ? 0.88 : 0.48) * (1 - seg * 0.25);
          const pRad = (isHi ? 2.1 : 1.4) * (1 - seg * 0.18);
          ctx.fillStyle = seg === 0 && isAbyss ? `rgba(255,255,255,${pAlpha})` : rgba(rgb, pAlpha);
          ctx.beginPath();
          ctx.arc(pos.x, pos.y, pRad, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    });

    // Burst energy waves
    for (let i = state.burstPackets.length - 1; i >= 0; i--) {
      const bp = state.burstPackets[i];
      bp.t += bp.speed * bp.dir;
      if (bp.t < 0 || bp.t > 1) {
        const targetId = bp.dir > 0 ? bp.link.target : bp.link.source;
        state.shockwaves.push({ nodeId: targetId, progress: 0, maxRadius: 40 });
        state.burstPackets.splice(i, 1);
        continue;
      }
      const u = state.nodeMap.get(bp.link.source);
      const v = state.nodeMap.get(bp.link.target);
      if (!u || !v) continue;
      const ctrl = curveControlPoint(u, v, bp.link.curveSign, cx, cy);
      const pos = quadPoint({ x: u.projX, y: u.projY }, ctrl, { x: v.projX, y: v.projY }, bp.t);
      const rMeta = REL_META[bp.link.relType] || REL_META.semantic;
      const rgb = isAbyss ? rMeta.abyss : rMeta.crystal;

      const glow = ctx.createRadialGradient(pos.x, pos.y, 0.5, pos.x, pos.y, 12);
      glow.addColorStop(0, "rgba(255,255,255,0.98)");
      glow.addColorStop(0.42, rgba(rgb, 0.65));
      glow.addColorStop(1, rgba(rgb, 0));
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(pos.x, pos.y, 12, 0, Math.PI * 2);
      ctx.fill();
    }

    // Gravitational ripple waves
    for (let i = state.shockwaves.length - 1; i >= 0; i--) {
      const sw = state.shockwaves[i];
      sw.progress += 0.024;
      if (sw.progress >= 1) {
        state.shockwaves.splice(i, 1);
        continue;
      }
      const n = state.nodeMap.get(sw.nodeId);
      if (!n) continue;
      const cm = clusterOf(n.project);
      const rgb = isAbyss ? cm.abyssColor : cm.crystalColor;
      const rad = 10 + sw.progress * sw.maxRadius * n.projScale;
      ctx.beginPath();
      ctx.arc(n.projX, n.projY, rad, 0, Math.PI * 2);
      ctx.strokeStyle = rgba(rgb, (1 - sw.progress) * 0.58);
      ctx.lineWidth = 1.4 * (1 - sw.progress * 0.5);
      ctx.stroke();
    }

    // Central Pulsar centroid in screen space (used as primary starlight source for 3D planets!)
    const pulsarCore = project3D(0, -6, 0, cx, cy);

    // ==================== LAYER 5: 3D CELESTIAL WORLDS & PULSARS ====================
    const sortedBackFirst = [...state.nodes].sort((a, b) => b.projZ - a.projZ);

    sortedBackFirst.forEach((n) => {
      const cm = clusterOf(n.project);
      const rgb = isAbyss ? cm.abyssColor : cm.crystalColor;
      const isSel = n.id === state.selectedId;
      const isHov = n.id === state.hoveredId;
      const isNeighbor = connectedIds.has(n.id);

      let dimFactor = 1;
      if (!n.matchesFilter) dimFactor = 0.15;
      else if (hoverOnly && !isNeighbor) dimFactor = 0.36;

      const r = Math.max(5.5, n.baseRadius * n.projScale);

      ctx.save();
      ctx.globalAlpha = dimFactor;

      if (n.kind === "skill") {
        drawPulsarStar(ctx, n, n.projX, n.projY, r, cm, isAbyss, tSec, isSel || isHov);
      } else {
        drawPlanetWorld(ctx, n, n.projX, n.projY, r, pulsarCore.x, pulsarCore.y, cm, isAbyss, tSec);
      }

      if (isSel || isHov) {
        drawOpticalReticle(ctx, n.projX, n.projY, r * 1.95, rgb, isAbyss, tSec, isSel);
      }

      ctx.restore();
    });

    // ==================== LAYER 6: FRAMELESS HOLOGRAPHIC STAR-CHART CALLOUTS ====================
    // Hysteresis-stabilized front worlds so rotation never causes rapid label flip-flop
    const frontMemories = [...state.nodes]
      .filter((n) => n.kind === "memory" && n.matchesFilter)
      .sort((a, b) => {
        const za = a.projZ - ((state.labelAlphas.get(a.id) || 0) > 0.45 ? 24 : 0);
        const zb = b.projZ - ((state.labelAlphas.get(b.id) || 0) > 0.45 ? 24 : 0);
        return za - zb;
      });
    const topFrontSet = new Set(frontMemories.slice(0, 4).map((n) => n.id));

    // Order nodes by callout priority: Focused -> Pulsars -> Currently visible -> Front-most
    const prioritizedNodes = [...sortedFrontFirst].sort((a, b) => {
      const aFoc = a.id === state.selectedId || a.id === state.hoveredId ? 1 : 0;
      const bFoc = b.id === state.selectedId || b.id === state.hoveredId ? 1 : 0;
      if (aFoc !== bFoc) return bFoc - aFoc;
      const aSk = a.kind === "skill" ? 1 : 0;
      const bSk = b.kind === "skill" ? 1 : 0;
      if (aSk !== bSk) return bSk - aSk;
      return a.projZ - b.projZ;
    });

    const calloutItems = [];
    const occupiedRects = [];

    prioritizedNodes.forEach((n) => {
      const cm = clusterOf(n.project);
      const rgb = isAbyss ? cm.abyssColor : cm.crystalColor;
      const isSel = n.id === state.selectedId;
      const isHov = n.id === state.hoveredId;
      const isFocus = isSel || isHov;
      const isNeighbor = hoverOnly ? connectedIds.has(n.id) : false;

      let targetAlpha = 0;
      if (!n.matchesFilter) {
        targetAlpha = 0;
      } else if (isFocus) {
        targetAlpha = 1.0;
      } else if (hoverOnly) {
        targetAlpha = isNeighbor ? 0.90 : 0.0;
      } else if (state.labelMode === "hover") {
        targetAlpha = 0.0;
      } else if (state.labelMode === "all") {
        targetAlpha = n.kind === "skill" ? 0.96 : 0.88;
      } else {
        if (n.kind === "skill") targetAlpha = 0.95;
        else if (topFrontSet.has(n.id)) targetAlpha = 0.90;
      }

      const r = Math.max(5.5, n.baseRadius * n.projScale);
      const prevA = state.labelAlphas.get(n.id);
      let chosenSlot = null;

      if (targetAlpha > 0 || (prevA && prevA > 0.04)) {
        const goRight = n.projX >= cx ? 1 : -1;
        const goDown = n.projY >= cy + 6 ? 1 : -1;
        const dirs = [
          [goRight, goDown],
          [goRight, -goDown],
          [-goRight, goDown],
          [-goRight, -goDown]
        ];

        let bestSlot = null;
        let bestPenalty = Infinity;
        for (let k = 0; k < dirs.length; k++) {
          const cand = buildCalloutSlot(ctx, n, n.projX, n.projY, r, W, H, isFocus, dirs[k][0], dirs[k][1]);
          const hitsLabel = occupiedRects.some((box) => rectsOverlap(cand.rect, box));
          if (hitsLabel && !isFocus) continue;

          // Mild penalty if text crosses over another planet sphere
          let planetHits = 0;
          for (let m = 0; m < state.nodes.length; m++) {
            const other = state.nodes[m];
            if (other.id === n.id || !other.matchesFilter) continue;
            const orad = Math.max(6, other.baseRadius * other.projScale) + 4;
            if (
              other.projX + orad > cand.rect.l &&
              other.projX - orad < cand.rect.r &&
              other.projY + orad > cand.rect.t &&
              other.projY - orad < cand.rect.b
            ) {
              planetHits += 1;
            }
          }
          const penalty = (hitsLabel ? 100 : 0) + planetHits * 10 + k * 2;
          if (penalty < bestPenalty) {
            bestPenalty = penalty;
            bestSlot = cand;
            if (penalty === 0) break;
          }
        }

        if (bestSlot) {
          chosenSlot = bestSlot;
        } else if (!isFocus) {
          targetAlpha = 0;
        }
      }

      let nextA = prevA === undefined ? targetAlpha : prevA + (targetAlpha - prevA) * 0.18;
      // Never draw a fading ghost label if all 4 quadrants around it collided with active labels
      if (!chosenSlot && !isFocus) {
        nextA = 0;
      }
      state.labelAlphas.set(n.id, nextA);

      if (nextA > 0.03 && chosenSlot) {
        occupiedRects.push(chosenSlot.rect);
        calloutItems.push({
          node: n,
          x: n.projX,
          y: n.projY,
          r,
          rgb,
          alpha: nextA,
          isFocus,
          slot: chosenSlot
        });
      }
    });

    // Draw background callouts first, focused callout on top
    calloutItems
      .sort((a, b) => (a.isFocus ? 1 : 0) - (b.isFocus ? 1 : 0))
      .forEach((item) => drawStarChartCallout(ctx, item, isAbyss));

    ctx.restore();
    state.rafId = window.requestAnimationFrame(drawFrame);
  }

  function resizeCanvas() {
    if (!state.canvas || !state.canvas.parentElement) return;
    const parent = state.canvas.parentElement;
    const w = Math.max(320, Math.floor(parent.clientWidth || 840));
    const h = Math.min(680, Math.max(420, Math.floor(parent.clientHeight || 540)));
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const targetW = Math.floor(w * dpr);
    const targetH = Math.floor(h * dpr);

    state.width = w;
    state.height = h;
    state.dpr = dpr;

    if (state.canvas.width !== targetW || state.canvas.height !== targetH || !state.ctx) {
      state.canvas.width = targetW;
      state.canvas.height = targetH;
      state.ctx = state.canvas.getContext("2d");
    }
  }

  function bindCanvasEvents(canvas) {
    canvas.addEventListener("pointerdown", (e) => {
      state.dragging = true;
      state.dragMoved = false;
      state.lastPtrX = e.clientX;
      state.lastPtrY = e.clientY;
      canvas.setPointerCapture(e.pointerId);
      canvas.style.cursor = "grabbing";
    });

    canvas.addEventListener("pointermove", (e) => {
      const rect = canvas.getBoundingClientRect();
      state.mouseX = e.clientX - rect.left;
      state.mouseY = e.clientY - rect.top;

      if (!state.dragging) return;
      const dx = e.clientX - state.lastPtrX;
      const dy = e.clientY - state.lastPtrY;
      if (Math.abs(dx) + Math.abs(dy) > 3) state.dragMoved = true;
      state.lastPtrX = e.clientX;
      state.lastPtrY = e.clientY;

      state.targetRotY += dx * 0.0065;
      state.targetRotX = Math.max(-0.82, Math.min(0.82, state.targetRotX + dy * 0.0055));
    });

    const endDrag = (e) => {
      if (!state.dragging) return;
      state.dragging = false;
      canvas.style.cursor = state.hoveredId ? "pointer" : "grab";
      if (!state.dragMoved && state.hoveredId) {
        focusNodeCamera(state.hoveredId, true);
      }
      try {
        canvas.releasePointerCapture(e.pointerId);
      } catch (_) {}
    };

    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);
    canvas.addEventListener("pointerleave", () => {
      if (!state.dragging) {
        state.mouseX = -9999;
        state.mouseY = -9999;
        state.hoveredId = null;
      }
    });

    canvas.addEventListener(
      "wheel",
      (e) => {
        if (!e.ctrlKey && !e.metaKey && !e.altKey) return;
        e.preventDefault();
        const factor = e.deltaY > 0 ? -0.1 : 0.1;
        state.targetZoom = Math.max(0.72, Math.min(1.65, state.targetZoom + factor));
      },
      { passive: false }
    );
  }

  function renderInspector() {
    const box = document.getElementById("nebula-inspector");
    if (!box) return;
    const node = state.nodeMap.get(state.selectedId) || state.nodes[0];
    if (!node) {
      box.innerHTML = `<div class="nebula-empty">暂无星体数据</div>`;
      return;
    }

    const cm = clusterOf(node.project);
    const neighbors = [];
    state.links.forEach((l) => {
      if (l.source === node.id || l.target === node.id) {
        const otherId = l.source === node.id ? l.target : l.source;
        const other = state.nodeMap.get(otherId);
        if (other) neighbors.push({ link: l, other });
      }
    });

    const evoHtml = node.hasPendingEvo
      ? `
        <div class="nebula-evo-callout">
          <div>
            <strong>⚡ 环绕演化卫星：${esc(node.hasPendingEvo.badgeText)}</strong>
            <div style="font-size:12px;margin-top:2px">${esc(node.hasPendingEvo.title)}</div>
          </div>
          <button type="button" class="btn btn-evolve btn-sm" data-nebula-jump-evo="${esc(node.hasPendingEvo.id)}">裁决 →</button>
        </div>
      `
      : "";

    box.innerHTML = `
      <div class="nebula-insp-head">
        <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:6px">
          <span class="badge ${node.kind === "skill" ? "badge-evolve" : "badge-brand"}">
            ${node.kind === "skill" ? "✦ 核心吸积主星 (Skill)" : node.hasRing ? "🪐 环状知识行星" : "🌍 知识星球"}
          </span>
          <span class="badge mono">${esc(cm.short)}</span>
          <span class="badge badge-ok">置信度 ${(node.confidence * 100).toFixed(0)}%</span>
        </div>
        <h3>${esc(node.title)}</h3>
        <div class="meta-row" style="margin-top:4px">
          <span>${esc(node.subtitle)}</span>
          <span>引力星链 <strong>${neighbors.length}</strong> 条</span>
        </div>
      </div>

      ${evoHtml}

      <div class="nebula-summary-box">
        <div class="nebula-sec-title">核心可复用准则 / 知识凝练</div>
        <p>${esc(node.summary)}</p>
      </div>

      <div class="nebula-links-wrap">
        <div class="nebula-sec-title" style="display:flex;justify-content:space-between;align-items:center">
          <span>星云关联知识条 (${neighbors.length})</span>
          <span style="font-weight:400;color:var(--ink-muted)">点击跃迁至对应星球</span>
        </div>
        <div class="nebula-link-list">
          ${neighbors
            .map(({ link, other }) => {
              const rm = REL_META[link.relType] || REL_META.semantic;
              return `
                <button type="button" class="nebula-link-item" data-nebula-focus="${esc(other.id)}">
                  <div class="nebula-link-top">
                    <strong>${esc(other.shortLabel)}</strong>
                    <span class="badge ${rm.badgeCls}">${esc(link.label)}</span>
                  </div>
                  <div class="nebula-link-reason">${esc(link.reason)}</div>
                </button>
              `;
            })
            .join("")}
        </div>
      </div>

      <div class="nebula-insp-actions">
        <button type="button" class="btn btn-sm" data-nebula-pulse="${esc(node.id)}">✨ 激发引力共振</button>
        ${
          node.kind === "skill"
            ? `<button type="button" class="btn btn-primary btn-sm" data-nebula-jump-skill="${esc(node.id)}">前往 Skill 库查看 →</button>`
            : node.raw?.manualId
            ? `<button type="button" class="btn btn-primary btn-sm" data-nebula-jump-mem="${esc(node.id)}">前往经验库定位 →</button>`
            : `<button type="button" class="btn btn-primary btn-sm" data-nebula-jump-mem="${esc(node.id)}">前往进化工坊查看 →</button>`
        }
      </div>
    `;
  }

  function labelModeText() {
    if (state.labelMode === "hover") return "星标: 仅焦点";
    if (state.labelMode === "all") return "星标: 全部";
    return "星标: 智能星图";
  }

  function renderSectionHTML(db) {
    state.lensMode = "abyss";
    buildGraph(db);
    const memCount = (db.memories || []).filter((m) => m.status !== "archived").length;
    const skillCount = (db.skills || []).length;
    const linkCount = state.links.length;
    const projects = Array.from(new Set((db.memories || []).map((m) => m.project)));

    return `
      <section class="panel nebula-panel" id="nebula-panel-root">
        <div class="panel-head" style="margin-bottom:12px">
          <div>
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
              <h2>0. 演化知识星云图谱 · Neural Knowledge Nebula</h2>
              <span class="badge badge-brand mono">${memCount} 知识星球 · ${skillCount} 吸积主星 · ${linkCount} 星际引力链</span>
            </div>
            <p>中央 <strong>Skill 吸积脉冲主星</strong> 照亮四周三维 <strong>知识行星、土星光环与演化伴星</strong>，悬停或点击任意星球可沿全息引线展开真实知识关联。</p>
          </div>

          <div class="nebula-top-controls">
            <span class="badge badge-brand" style="padding:5px 11px;font-weight:600">🌌 冰渊星冕舱</span>
            <button type="button" class="btn btn-sm" id="btn-nebula-burst">✨ 激发星云脉冲</button>
          </div>
        </div>

        <div class="nebula-toolbar" id="nebula-toolbar">
          <div class="nebula-rel-pills" role="group" aria-label="知识关联类型筛选">
            <button type="button" class="subtab ${state.relFilter === "all" ? "active" : ""}" data-nebula-rel="all">全部关联</button>
            <button type="button" class="subtab ${state.relFilter === "distill" ? "active" : ""}" data-nebula-rel="distill">Skill 升华沉淀</button>
            <button type="button" class="subtab ${state.relFilter === "semantic" ? "active" : ""}" data-nebula-rel="semantic">同域语义关联</button>
            <button type="button" class="subtab ${state.relFilter === "lineage" ? "active" : ""}" data-nebula-rel="lineage">进化谱系派生</button>
            <button type="button" class="subtab ${state.relFilter === "bridge" ? "active" : ""}" data-nebula-rel="bridge">跨星域复用</button>
          </div>

          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <select id="nebula-proj-select" class="btn-sm" aria-label="知识星域过滤" style="min-height:32px">
              <option value="all" ${state.projectFilter === "all" ? "selected" : ""}>全部四大知识星域</option>
              ${projects.map((p) => `<option value="${esc(p)}" ${state.projectFilter === p ? "selected" : ""}>${esc(clusterOf(p).label)}</option>`).join("")}
            </select>
            <input type="search" id="nebula-search-input" value="${esc(state.searchQuery)}" placeholder="定位知识星球 (如 利润 / 按钮 / 续接)…" style="min-height:32px;width:230px;padding:4px 10px">
          </div>
        </div>

        <div class="nebula-stage-grid">
          <div class="nebula-viewport-wrap abyss-mode" id="nebula-viewport-wrap">
            <canvas id="knowledge-nebula-canvas" aria-label="EvolvMem 知识关联三维星云图"></canvas>

            <div class="nebula-hud-legend">
              <span><i class="legend-dot" style="background:#7dd3fc"></i>系统与续接</span>
              <span><i class="legend-dot" style="background:#6ee7b7"></i>前端与交互</span>
              <span><i class="legend-dot" style="background:#c4b5fd"></i>跨境业务</span>
              <span><i class="legend-dot" style="background:#fde68a"></i>飞书协同</span>
              <span><i class="legend-star">✦</i>吸积主星</span>
              <span>🪐 光环/演化伴星</span>
            </div>

            <div class="nebula-cam-dock">
              <button type="button" class="nebula-cam-btn" data-nebula-cam="cycle-labels" id="btn-nebula-labels" title="切换星图文字显示密度">
                ${labelModeText()}
              </button>
              <button type="button" class="nebula-cam-btn" data-nebula-cam="zoom-in" title="放大星云">＋</button>
              <button type="button" class="nebula-cam-btn" data-nebula-cam="zoom-out" title="缩小星云">－</button>
              <button type="button" class="nebula-cam-btn" data-nebula-cam="toggle-rot" id="btn-nebula-rot" title="切换自动巡航旋转">
                ${state.autoRotate ? "⏸ 巡航中" : "▶ 已定格"}
              </button>
              <button type="button" class="nebula-cam-btn" data-nebula-cam="reset" title="复位三维视角">复位</button>
            </div>
          </div>

          <aside class="nebula-inspector" id="nebula-inspector" aria-live="polite"></aside>
        </div>
      </section>
    `;
  }

  function updateToolbarUI() {
    const root = document.getElementById("nebula-panel-root");
    if (!root) return;
    root.querySelectorAll("[data-nebula-lens]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.nebulaLens === state.lensMode);
    });
    root.querySelectorAll("[data-nebula-rel]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.nebulaRel === state.relFilter);
    });
    const wrap = document.getElementById("nebula-viewport-wrap");
    if (wrap) {
      wrap.classList.toggle("crystal-mode", state.lensMode === "crystal");
      wrap.classList.toggle("abyss-mode", state.lensMode === "abyss");
    }
    const rotBtn = document.getElementById("btn-nebula-rot");
    if (rotBtn) rotBtn.textContent = state.autoRotate ? "⏸ 巡航中" : "▶ 已定格";
    const lblBtn = document.getElementById("btn-nebula-labels");
    if (lblBtn) lblBtn.textContent = labelModeText();
  }

  function mount(db, callbacks = {}) {
    unmount();
    state.callbacks = callbacks;
    buildGraph(db);

    const canvas = document.getElementById("knowledge-nebula-canvas");
    if (!canvas) return;
    state.canvas = canvas;
    resizeCanvas();
    bindCanvasEvents(canvas);

    if (typeof ResizeObserver !== "undefined") {
      state.resizeObs = new ResizeObserver(() => resizeCanvas());
      state.resizeObs.observe(canvas.parentElement);
    }

    const projSel = document.getElementById("nebula-proj-select");
    if (projSel) {
      projSel.onchange = (e) => {
        state.projectFilter = e.target.value;
      };
    }

    const searchInp = document.getElementById("nebula-search-input");
    if (searchInp) {
      searchInp.oninput = (e) => {
        state.searchQuery = e.target.value.trim();
        if (state.searchQuery) {
          const hit = state.nodes.find((n) => isNodeMatchingFilter(n));
          if (hit) focusNodeCamera(hit.id, false);
        }
      };
    }

    renderInspector();
    state.rafId = window.requestAnimationFrame(drawFrame);
  }

  function unmount() {
    if (state.rafId) {
      window.cancelAnimationFrame(state.rafId);
      state.rafId = 0;
    }
    if (state.resizeObs) {
      state.resizeObs.disconnect();
      state.resizeObs = null;
    }
    state.canvas = null;
    state.ctx = null;
  }

  document.addEventListener("click", (e) => {
    const lensBtn = e.target.closest("[data-nebula-lens]");
    if (lensBtn) {
      state.lensMode = lensBtn.dataset.nebulaLens;
      updateToolbarUI();
      return;
    }

    const relBtn = e.target.closest("[data-nebula-rel]");
    if (relBtn) {
      state.relFilter = relBtn.dataset.nebulaRel;
      updateToolbarUI();
      return;
    }

    const focusBtn = e.target.closest("[data-nebula-focus]");
    if (focusBtn) {
      focusNodeCamera(focusBtn.dataset.nebulaFocus, true);
      return;
    }

    const pulseBtn = e.target.closest("[data-nebula-pulse]");
    if (pulseBtn) {
      triggerStarResonance(pulseBtn.dataset.nebulaPulse);
      return;
    }

    if (e.target.closest("#btn-nebula-burst")) {
      state.nodes
        .filter((n) => n.kind === "skill")
        .forEach((sk) => triggerStarResonance(sk.id));
      return;
    }

    const camBtn = e.target.closest("[data-nebula-cam]");
    if (camBtn) {
      const act = camBtn.dataset.nebulaCam;
      if (act === "zoom-in") state.targetZoom = Math.min(1.65, state.targetZoom + 0.16);
      else if (act === "zoom-out") state.targetZoom = Math.max(0.72, state.targetZoom - 0.16);
      else if (act === "toggle-rot") {
        state.autoRotate = !state.autoRotate;
        updateToolbarUI();
      } else if (act === "cycle-labels") {
        state.labelMode =
          state.labelMode === "smart" ? "hover" : state.labelMode === "hover" ? "all" : "smart";
        updateToolbarUI();
      } else if (act === "reset") {
        state.targetRotX = 0.24;
        state.targetRotY = -0.32;
        state.targetZoom = 1.0;
        state.projectFilter = "all";
        state.relFilter = "all";
        state.searchQuery = "";
        const sel = document.getElementById("nebula-proj-select");
        if (sel) sel.value = "all";
        const inp = document.getElementById("nebula-search-input");
        if (inp) inp.value = "";
        updateToolbarUI();
      }
      return;
    }

    const jumpMem = e.target.closest("[data-nebula-jump-mem]");
    if (jumpMem && state.callbacks?.onJumpMemory) {
      const n = state.nodeMap.get(jumpMem.dataset.nebulaJumpMem);
      state.callbacks.onJumpMemory(n ? n.raw : jumpMem.dataset.nebulaJumpMem);
      return;
    }

    const jumpSkill = e.target.closest("[data-nebula-jump-skill]");
    if (jumpSkill && state.callbacks?.onJumpSkill) {
      const n = state.nodeMap.get(jumpSkill.dataset.nebulaJumpSkill);
      state.callbacks.onJumpSkill(n ? n.raw : jumpSkill.dataset.nebulaJumpSkill);
      return;
    }

    const jumpEvo = e.target.closest("[data-nebula-jump-evo]");
    if (jumpEvo && state.callbacks?.onJumpEvolution) {
      state.callbacks.onJumpEvolution(jumpEvo.dataset.nebulaJumpEvo);
    }
  });

  window.EvolvMemNebula = {
    renderSectionHTML,
    mount,
    unmount,
    focusNodeCamera,
    triggerStarResonance,
    buildGraph
  };
})();
