import { useEffect, useRef, useState } from 'react';
import {
  ArrowDownRight, ArrowRight, Braces, Check, ChevronRight,
  CircleDot, Command, FileSearch, Fingerprint, Globe2,
  MousePointer2, ScanSearch, ShieldCheck, Sparkles, Zap,
} from 'lucide-react';
import './landing.css';

/* ─────────────────── static data ─────────────────── */
const capabilities = [
  { icon: Globe2,        number: '01', title: 'Navigate the web',      copy: 'Move through real websites, tabs, and multi-step workflows with an agent that understands your goal.' },
  { icon: MousePointer2, number: '02', title: 'Act with precision',     copy: 'Click, type, select, and complete forms through browser actions—not brittle, hard-coded scripts.' },
  { icon: FileSearch,    number: '03', title: 'Extract what matters',   copy: 'Turn page content into structured information you can inspect, reuse, and act on.' },
  { icon: ShieldCheck,   number: '04', title: 'Keep humans in control', copy: 'Review sensitive actions, approve important steps, and follow the execution as it happens.' },
];

const endpointGroups = [
  {
    title: 'Task automation',
    copy: 'Create, monitor, approve, and manage browser workflows.',
    endpoints: [
      ['POST', '/api/tasks', 'Create and launch an automation task'],
      ['GET', '/api/tasks', 'List active tasks'],
      ['GET', '/api/tasks/history', 'Retrieve task history'],
      ['GET', '/api/tasks/{task_id}', 'Get task status and execution details'],
      ['POST', '/api/tasks/{task_id}/approve', 'Approve a pending action'],
      ['POST', '/api/tasks/{task_id}/cancel', 'Cancel a running task'],
    ],
  },
  {
    title: 'Browser & extraction',
    copy: 'Check browser connectivity and collect structured page data.',
    endpoints: [
      ['GET', '/api/pool/status', 'Get browser pool status'],
      ['GET', '/api/extension/status', 'Check Chrome extension connection'],
      ['POST', '/api/extract', 'Extract structured information'],
      ['POST', '/api/forms/discover', 'Discover forms and input fields'],
    ],
  },
  {
    title: 'Research',
    copy: 'Run multi-page research and retrieve its results.',
    endpoints: [
      ['POST', '/api/research', 'Start a research task'],
      ['GET', '/api/research/{task_id}', 'Get research task status'],
      ['GET', '/api/research/{task_id}/results', 'Fetch comparison results'],
      ['GET', '/api/research/{task_id}/export', 'Export research results'],
    ],
  },
];

const sampleSteps = [
  { icon: Globe2,      label: 'Open target website',    status: 'done'    },
  { icon: ScanSearch,  label: 'Inspect page structure', status: 'done'    },
  { icon: Braces,      label: 'Build an action plan',   status: 'active'  },
  { icon: ShieldCheck, label: 'Wait for your approval', status: 'pending' },
];

/* ─────────────────── canvas node graph ──────────────── */
const NODES = [
  { id: 'browser',   label: 'Browser Control',  sub: 'Playwright engine', angle: -0.35, baseDist: 170, r: 16, ringColor: '#65e6a4', main: false },
  { id: 'planner',   label: 'Action Planner',   sub: 'LLM reasoning',     angle:  0.80, baseDist: 195, r: 15, ringColor: '#65e6a4', main: false },
  { id: 'extractor', label: 'DOM Extraction',   sub: 'Structured data',   angle:  2.05, baseDist: 165, r: 14, ringColor: '#58dca1', main: false },
  { id: 'approval',  label: 'Human Approval',   sub: 'Safety checkpoint', angle:  3.25, baseDist: 205, r: 17, ringColor: '#7cf0b5', main: false },
  { id: 'recovery',  label: 'Self Healing',     sub: 'Error recovery',    angle:  4.35, baseDist: 155, r: 13, ringColor: '#54d89b', main: false },
  { id: 'tabs',      label: 'Tab Manager',      sub: 'Multi-session',     angle:  5.40, baseDist: 185, r: 14, ringColor: '#65e6a4', main: false },
];

const CONNECTORS = [
  [0, 1], // Browser <-> Planner
  [1, 2], // Planner <-> Extractor
  [0, 3], // Browser <-> Approval
  [2, 4], // Extractor <-> Recovery
  [0, 5], // Browser <-> Tab Manager
  [3, 4], // Approval <-> Recovery
];

function NodeCanvas() {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let animId;
    let phase = 0;
    let lastScrollY = window.scrollY;
    let scrollVelocity = 0;
    let smoothScroll = window.scrollY;

    const mouseTarget = { x: 0.5, y: 0.5, active: false, px: 0, py: 0 };
    const mouseSmooth = { x: 0.5, y: 0.5 };

    const onMouseMove = (e) => {
      const rect = canvas.getBoundingClientRect();
      mouseTarget.x = (e.clientX - rect.left) / (rect.width || 1);
      mouseTarget.y = (e.clientY - rect.top) / (rect.height || 1);
      mouseTarget.px = e.clientX - rect.left;
      mouseTarget.py = e.clientY - rect.top;
      mouseTarget.active = true;
    };

    const onMouseLeave = () => {
      mouseTarget.active = false;
      mouseTarget.x = 0.5;
      mouseTarget.y = 0.5;
    };

    const onScroll = () => {
      const current = window.scrollY;
      scrollVelocity = (current - lastScrollY) * 0.4;
      lastScrollY = current;
    };

    window.addEventListener('mousemove', onMouseMove, { passive: true });
    window.addEventListener('mouseleave', onMouseLeave, { passive: true });
    window.addEventListener('scroll', onScroll, { passive: true });

    let currentW = 0;
    let currentH = 0;

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = Math.max(Math.floor(rect.width || canvas.parentElement?.clientWidth || 500), 300);
      const h = Math.max(Math.floor(rect.height || canvas.parentElement?.clientHeight || 500), 300);

      if (currentW !== w || currentH !== h) {
        currentW = w;
        currentH = h;
        canvas.width = Math.floor(w * dpr);
        canvas.height = Math.floor(h * dpr);
      }
    };

    resize();
    const ro = new ResizeObserver(() => resize());
    ro.observe(canvas);
    if (canvas.parentElement) ro.observe(canvas.parentElement);

    // Micro stars for deep space background
    const stars = Array.from({ length: 28 }, (_, i) => ({
      angle: (i / 28) * Math.PI * 2,
      dist: 60 + (i * 17) % 200,
      r: (i % 3 === 0 ? 1.8 : 1.2),
      alpha: 0.2 + (i % 5) * 0.12,
      speed: (i % 2 === 0 ? 1 : -1) * (0.001 + (i % 3) * 0.0008),
    }));

    const draw = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const W = currentW;
      const H = currentH;

      ctx.save();
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);

      phase += 0.012;
      smoothScroll += (window.scrollY - smoothScroll) * 0.08;
      scrollVelocity *= 0.92;

      mouseSmooth.x += (mouseTarget.x - mouseSmooth.x) * 0.06;
      mouseSmooth.y += (mouseTarget.y - mouseSmooth.y) * 0.06;

      // Parallax offsets
      const mouseParallaxX = (mouseSmooth.x - 0.5) * 44;
      const mouseParallaxY = (mouseSmooth.y - 0.5) * 34;

      // Center hub coordinates
      const cx = W * 0.5 + mouseParallaxX * 0.4;
      const cy = H * 0.48 + mouseParallaxY * 0.4;

      // Interactive scroll expansion: nodes gently spread and rotate when scrolling
      const scrollRotation = smoothScroll * 0.0022;
      const scrollSpread = 1 + Math.min(Math.max(smoothScroll * 0.0007, 0), 0.38) + Math.min(Math.abs(scrollVelocity) * 0.015, 0.25);

      /* ── 1. Orbital Ring Constellation ── */
      const orbitRadii = [90, 165, 230];
      orbitRadii.forEach((baseR, idx) => {
        const r = baseR * scrollSpread;
        ctx.beginPath();
        // subtle 3D tilt
        ctx.ellipse(cx, cy, r, r * 0.82, -0.15 + (mouseSmooth.x - 0.5) * 0.2, 0, Math.PI * 2);
        ctx.strokeStyle = idx === 1 ? 'rgba(101, 230, 164, 0.14)' : 'rgba(101, 230, 164, 0.06)';
        ctx.lineWidth = 1;
        ctx.setLineDash(idx === 1 ? [4, 6] : [2, 10]);
        ctx.lineDashOffset = -phase * (idx + 1) * 6;
        ctx.stroke();
        ctx.setLineDash([]);
      });

      /* ── 2. Ambient Micro-stars ── */
      stars.forEach((s) => {
        s.angle += s.speed;
        const sx = cx + Math.cos(s.angle + scrollRotation * 0.5) * s.dist * scrollSpread;
        const sy = cy + Math.sin(s.angle + scrollRotation * 0.5) * (s.dist * 0.82) * scrollSpread;
        ctx.beginPath();
        ctx.arc(sx, sy, s.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(101, 230, 164, ${s.alpha * 0.7})`;
        ctx.fill();
      });

      /* ── 3. Calculate Node Coordinates ── */
      const computedNodes = NODES.map((nd, idx) => {
        // Individual rotation + scroll rotation + gentle floating oscillation
        const curAngle = nd.angle + phase * 0.18 + scrollRotation;
        const floatX = Math.cos(phase * 1.5 + idx * 1.3) * 6;
        const floatY = Math.sin(phase * 1.2 + idx * 0.9) * 5;

        const effectiveDist = nd.baseDist * scrollSpread;
        const nx = cx + Math.cos(curAngle) * effectiveDist + floatX + mouseParallaxX * 0.7;
        const ny = cy + Math.sin(curAngle) * (effectiveDist * 0.82) + floatY + mouseParallaxY * 0.7;

        // Hover test
        let isHovered = false;
        if (mouseTarget.active) {
          const dx = mouseTarget.px - nx;
          const dy = mouseTarget.py - ny;
          if (Math.hypot(dx, dy) < 42) isHovered = true;
        }

        return { ...nd, nx, ny, isHovered };
      });

      /* ── 4. Connecting Edges & Energy Pulses ── */
      // Hub to Nodes
      computedNodes.forEach((node, i) => {
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.lineTo(node.nx, node.ny);
        const grad = ctx.createLinearGradient(cx, cy, node.nx, node.ny);
        grad.addColorStop(0, 'rgba(101, 230, 164, 0.45)');
        grad.addColorStop(0.7, 'rgba(101, 230, 164, 0.12)');
        grad.addColorStop(1, 'rgba(101, 230, 164, 0.03)');
        ctx.strokeStyle = grad;
        ctx.lineWidth = node.isHovered ? 1.8 : 1;
        ctx.stroke();

        // Traveling energy packet
        const t = ((phase * 0.5 + i * 0.35 + smoothScroll * 0.003) % 1 + 1) % 1;
        const qx = cx + (node.nx - cx) * t;
        const qy = cy + (node.ny - cy) * t;

        ctx.beginPath();
        ctx.arc(qx, qy, node.isHovered ? 3.5 : 2.2, 0, Math.PI * 2);
        ctx.fillStyle = '#65e6a4';
        ctx.shadowColor = '#65e6a4';
        ctx.shadowBlur = 10;
        ctx.fill();
        ctx.shadowBlur = 0;
      });

      // Inter-node connectors
      CONNECTORS.forEach(([aIdx, bIdx]) => {
        const na = computedNodes[aIdx];
        const nb = computedNodes[bIdx];
        if (!na || !nb) return;

        ctx.beginPath();
        ctx.moveTo(na.nx, na.ny);
        ctx.lineTo(nb.nx, nb.ny);
        ctx.strokeStyle = 'rgba(101, 230, 164, 0.10)';
        ctx.lineWidth = 1;
        ctx.stroke();

        // Cross-packet
        const t2 = ((phase * 0.4 + aIdx * 0.5) % 1 + 1) % 1;
        const px2 = na.nx + (nb.nx - na.nx) * t2;
        const py2 = na.ny + (nb.ny - na.ny) * t2;
        ctx.beginPath();
        ctx.arc(px2, py2, 1.8, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(180, 250, 215, 0.75)';
        ctx.fill();
      });

      /* ── 5. Central Hub (Agent Core) ── */
      const hubPulse = 1 + Math.sin(phase * 2) * 0.08;
      const hubR = 32 * hubPulse;

      // Core outer aura
      const hubHalo = ctx.createRadialGradient(cx, cy, 0, cx, cy, hubR * 2.8);
      hubHalo.addColorStop(0, 'rgba(101, 230, 164, 0.28)');
      hubHalo.addColorStop(0.5, 'rgba(101, 230, 164, 0.08)');
      hubHalo.addColorStop(1, 'transparent');
      ctx.beginPath();
      ctx.arc(cx, cy, hubR * 2.8, 0, Math.PI * 2);
      ctx.fillStyle = hubHalo;
      ctx.fill();

      // Outer radar ring
      ctx.beginPath();
      ctx.arc(cx, cy, hubR + 10, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(101, 230, 164, 0.45)';
      ctx.lineWidth = 1;
      ctx.stroke();

      // Rotating tick marks
      for (let k = 0; k < 6; k++) {
        const tickA = phase * 0.8 + (k * Math.PI) / 3;
        const tx1 = cx + Math.cos(tickA) * (hubR + 8);
        const ty1 = cy + Math.sin(tickA) * (hubR + 8);
        const tx2 = cx + Math.cos(tickA) * (hubR + 14);
        const ty2 = cy + Math.sin(tickA) * (hubR + 14);
        ctx.beginPath();
        ctx.moveTo(tx1, ty1);
        ctx.lineTo(tx2, ty2);
        ctx.strokeStyle = 'rgba(101, 230, 164, 0.8)';
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }

      // Hub body
      ctx.beginPath();
      ctx.arc(cx, cy, hubR, 0, Math.PI * 2);
      ctx.fillStyle = '#08120c';
      ctx.fill();
      ctx.strokeStyle = '#65e6a4';
      ctx.lineWidth = 2;
      ctx.shadowColor = '#65e6a4';
      ctx.shadowBlur = 12;
      ctx.stroke();
      ctx.shadowBlur = 0;

      // Hub inner symbol (Crosshair / Agent Core)
      ctx.beginPath();
      ctx.arc(cx, cy, 8, 0, Math.PI * 2);
      ctx.fillStyle = '#65e6a4';
      ctx.fill();

      ctx.beginPath();
      ctx.moveTo(cx - 16, cy); ctx.lineTo(cx + 16, cy);
      ctx.moveTo(cx, cy - 16); ctx.lineTo(cx, cy + 16);
      ctx.strokeStyle = 'rgba(101, 230, 164, 0.5)';
      ctx.lineWidth = 1.2;
      ctx.stroke();

      /* ── 6. Render Satellite Nodes ── */
      computedNodes.forEach((nd, i) => {
        const nodePulse = 1 + Math.sin(phase * 1.8 + i) * 0.1;
        const r = (nd.isHovered ? nd.r * 1.25 : nd.r) * nodePulse;

        // Node Glow
        const nGlow = ctx.createRadialGradient(nd.nx, nd.ny, 0, nd.nx, nd.ny, r * 2.8);
        nGlow.addColorStop(0, nd.isHovered ? 'rgba(101, 230, 164, 0.38)' : 'rgba(101, 230, 164, 0.14)');
        nGlow.addColorStop(1, 'transparent');
        ctx.beginPath();
        ctx.arc(nd.nx, nd.ny, r * 2.8, 0, Math.PI * 2);
        ctx.fillStyle = nGlow;
        ctx.fill();

        // Node Ring
        ctx.beginPath();
        ctx.arc(nd.nx, nd.ny, r, 0, Math.PI * 2);
        ctx.strokeStyle = nd.isHovered ? '#65e6a4' : 'rgba(101, 230, 164, 0.65)';
        ctx.lineWidth = nd.isHovered ? 2 : 1.2;
        ctx.stroke();

        // Node Body
        ctx.beginPath();
        ctx.arc(nd.nx, nd.ny, r - 1.5, 0, Math.PI * 2);
        ctx.fillStyle = nd.isHovered ? '#0b1d12' : '#08120b';
        ctx.fill();

        // Node center pip
        ctx.beginPath();
        ctx.arc(nd.nx, nd.ny, 3.2, 0, Math.PI * 2);
        ctx.fillStyle = '#65e6a4';
        ctx.fill();

        // Sleek Label Tag Pill
        const title = nd.label;
        const sub = nd.sub;
        ctx.font = '500 11px "DM Sans", sans-serif';
        const titleW = ctx.measureText(title).width;
        ctx.font = '9px "DM Mono", monospace';
        const subW = ctx.measureText(sub).width;

        const pillW = Math.max(titleW, subW) + 26;
        const pillH = 32;

        // Place pill outward from center
        const angleFromCenter = Math.atan2(nd.ny - cy, nd.nx - cx);
        const pillX = nd.nx + Math.cos(angleFromCenter) * (r + 14) - (Math.cos(angleFromCenter) < 0 ? pillW : 0);
        const pillY = nd.ny + Math.sin(angleFromCenter) * (r + 14) - pillH / 2;

        // Pill rounded rect
        const pr = 6;
        ctx.beginPath();
        ctx.moveTo(pillX + pr, pillY);
        ctx.lineTo(pillX + pillW - pr, pillY);
        ctx.quadraticCurveTo(pillX + pillW, pillY, pillX + pillW, pillY + pr);
        ctx.lineTo(pillX + pillW, pillY + pillH - pr);
        ctx.quadraticCurveTo(pillX + pillW, pillY + pillH, pillX + pillW - pr, pillY + pillH);
        ctx.lineTo(pillX + pr, pillY + pillH);
        ctx.quadraticCurveTo(pillX, pillY + pillH, pillX, pillY + pillH - pr);
        ctx.lineTo(pillX, pillY + pr);
        ctx.quadraticCurveTo(pillX, pillY, pillX + pr, pillY);
        ctx.closePath();

        ctx.fillStyle = nd.isHovered ? 'rgba(10, 24, 15, 0.95)' : 'rgba(7, 14, 10, 0.88)';
        ctx.fill();
        ctx.strokeStyle = nd.isHovered ? 'rgba(101, 230, 164, 0.6)' : 'rgba(101, 230, 164, 0.22)';
        ctx.lineWidth = 1;
        ctx.stroke();

        // Indicator dot in pill
        ctx.beginPath();
        ctx.arc(pillX + 11, pillY + 11, 2.5, 0, Math.PI * 2);
        ctx.fillStyle = '#65e6a4';
        ctx.shadowColor = '#65e6a4';
        ctx.shadowBlur = 6;
        ctx.fill();
        ctx.shadowBlur = 0;

        // Title text
        ctx.fillStyle = '#eff3ef';
        ctx.font = '600 11px "DM Sans", sans-serif';
        ctx.textAlign = 'left';
        ctx.fillText(title, pillX + 18, pillY + 14);

        // Subtext / role
        ctx.fillStyle = 'rgba(161, 215, 180, 0.75)';
        ctx.font = '9px "DM Mono", monospace';
        ctx.fillText(sub, pillX + 18, pillY + 26);
      });

      /* ── 7. Interactive Hover Cursor Tether ── */
      if (mouseTarget.active) {
        ctx.beginPath();
        ctx.arc(mouseTarget.px, mouseTarget.py, 4, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(101, 230, 164, 0.6)';
        ctx.fill();

        ctx.beginPath();
        ctx.arc(mouseTarget.px, mouseTarget.py, 10, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(101, 230, 164, 0.25)';
        ctx.lineWidth = 1;
        ctx.stroke();
      }

      ctx.restore();
      animId = requestAnimationFrame(draw);
    };

    animId = requestAnimationFrame(draw);

    return () => {
      cancelAnimationFrame(animId);
      ro.disconnect();
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseleave', onMouseLeave);
      window.removeEventListener('scroll', onScroll);
    };
  }, []);

  return <canvas ref={canvasRef} className="node-canvas" aria-label="Interactive AI browser agent node graph" />;
}

/* ─────────────────── scroll-reveal hook ─────────────── */
function useReveal(threshold = 0.15) {
  const ref = useRef(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      ([e]) => { if (e.isIntersecting) { setVisible(true); io.disconnect(); } },
      { threshold }
    );
    io.observe(el);
    return () => io.disconnect();
  }, [threshold]);
  return [ref, visible];
}

/* ─────────────────── main page ──────────────────────── */
export default function Landing({ onOpen }) {
  const [wfRef,  wfVis]  = useReveal(0.12);
  const [capRef, capVis] = useReveal(0.08);
  const [ctaRef, ctaVis] = useReveal(0.20);

  return (
    <div className="landing-shell">
      <nav className="landing-nav">
        <a className="landing-brand" href="#top" aria-label="AI Browser Agent home">
          <span className="landing-brand-mark"><Command size={19} /></span>
          <span>browser<span className="brand-accent">agent</span><small>AI AUTOMATION WORKSPACE</small></span>
        </a>
        <div className="landing-nav-links">
          <a href="#capabilities">Capabilities</a>
          <a href="#workflow">How it works</a>
          <a href="#api">API endpoints</a>
        </div>
        <button className="landing-open" onClick={onOpen}>Open workspace <ArrowRight size={16} /></button>
      </nav>

      <main id="top">

        {/* ── Hero ───────────────────────────────────── */}
        <section className="landing-hero">
          <div className="landing-hero-copy reveal-fade-up reveal-visible">
            <div className="landing-kicker"><span className="kicker-dot" /> YOUR BROWSER, WORKING FOR YOU</div>
            <h1>Give the web<br />a <em>little direction.</em></h1>
            <p className="landing-lede">
              An AI agent that turns plain-language instructions into real browser actions.
              Navigate, interact, extract, and automate—while you stay in control.
            </p>
            <div className="landing-actions">
              <button className="landing-primary" onClick={onOpen}>Launch the agent <ArrowRight size={17} /></button>
              <a className="landing-secondary" href="#capabilities">Explore capabilities <ArrowDownRight size={17} /></a>
            </div>
            <div className="landing-proof">
              <span><Check size={14} /> Natural-language tasks</span>
              <span><Check size={14} /> Live execution tracking</span>
              <span><Check size={14} /> Human-in-the-loop</span>
            </div>
          </div>

          <div className="hero-canvas-wrap">
            <NodeCanvas />
            <div className="orbit-caption">
              <span className="live-pulse" /> AUTONOMOUS WORKFLOWS <span>·</span> HUMAN CONTROL
            </div>
          </div>

          <div className="landing-scroll-cue"><span /> SCROLL TO EXPLORE</div>
        </section>

        {/* ── Workflow ────────────────────────────────── */}
        <section
          className={`landing-workflow${wfVis ? ' reveal-visible' : ''}`}
          id="workflow"
          ref={wfRef}
        >
          <div className="section-intro reveal-fade-up">
            <span className="landing-kicker">FROM PROMPT TO PROGRESS</span>
            <h2>Not another chatbot.<br /><em>An agent that gets things done.</em></h2>
            <p>Describe the outcome. Watch the plan take shape. Follow each action as it happens.</p>
          </div>

          <div className="workflow-preview reveal-fade-left">
            <div className="preview-topbar">
              <div className="window-dots"><i /><i /><i /></div>
              <span><CircleDot size={12} /> execution / live session</span>
              <span className="preview-live"><i /> LIVE PREVIEW</span>
            </div>
            <div className="preview-content">
              <div className="preview-prompt">
                <span className="prompt-icon"><Sparkles size={17} /></span>
                <div>
                  <small>YOUR INSTRUCTION</small>
                  <strong>Find the key details on this website and organize the results.</strong>
                </div>
                <span className="prompt-arrow"><ArrowRight size={16} /></span>
              </div>
              <div className="preview-status">
                <div><span className="status-pulse" /> <strong>Agent execution plan</strong></div>
                <span>STEP 03 OF 04</span>
              </div>
              <div className="preview-steps">
                {sampleSteps.map((step, si) => {
                  const Icon = step.icon;
                  return (
                    <div
                      className={'preview-step ' + step.status}
                      key={step.label}
                      style={{ transitionDelay: `${si * 80}ms` }}
                    >
                      <span className="preview-step-icon">
                        {step.status === 'done' ? <Check size={15} /> : <Icon size={15} />}
                      </span>
                      <span>{step.label}</span>
                      <span className="step-state">
                        {step.status === 'done' ? 'Completed' : step.status === 'active' ? 'In progress' : 'Upcoming'}
                      </span>
                    </div>
                  );
                })}
              </div>
              <div className="preview-foot">
                <span><Fingerprint size={14} /> Transparent by design</span>
                <span><ShieldCheck size={14} /> Approval when it matters</span>
              </div>
            </div>
          </div>
        </section>

        {/* ── Capabilities ────────────────────────────── */}
        <section
          className={`landing-capabilities${capVis ? ' reveal-visible' : ''}`}
          id="capabilities"
          ref={capRef}
        >
          <div className="capabilities-heading reveal-fade-up">
            <div>
              <span className="landing-kicker">BUILT FOR REAL WORK</span>
              <h2>From busywork<br />to <em>flow state.</em></h2>
            </div>
            <p>
              Useful browser automation should be powerful, observable, and predictable.
              Every capability is designed around that idea.
            </p>
          </div>
          <div className="capability-grid">
            {capabilities.map((item, ci) => {
              const Icon = item.icon;
              return (
                <article
                  className="capability-card reveal-fade-up"
                  key={item.number}
                  style={{ transitionDelay: `${ci * 90}ms` }}
                >
                  <div className="capability-card-top">
                    <span className="capability-icon"><Icon size={20} strokeWidth={1.6} /></span>
                    <span>{item.number}</span>
                  </div>
                  <h3>{item.title}</h3>
                  <p>{item.copy}</p>
                  <div className="capability-line"><span /></div>
                </article>
              );
            })}
          </div>
        </section>

        {/* ── API endpoints ──────────────────────────── */}
        <section className="landing-api" id="api">
          <div className="api-heading">
            <div>
              <span className="landing-kicker"><Braces size={13} /> DEVELOPER REFERENCE</span>
              <h2>Connect to the<br /><em>automation API.</em></h2>
            </div>
            <p>
              Explore the core REST endpoints powering task execution, browser
              connectivity, extraction, and research. All routes use the <code>/api</code> prefix.
            </p>
          </div>

          <div className="api-groups">
            {endpointGroups.map((group) => (
              <article className="api-group" key={group.title}>
                <div className="api-group-heading">
                  <div>
                    <h3>{group.title}</h3>
                    <p>{group.copy}</p>
                  </div>
                  <span>{String(group.endpoints.length).padStart(2, '0')} ROUTES</span>
                </div>
                <div className="api-endpoint-list">
                  {group.endpoints.map(([method, path, description]) => (
                    <div className="api-endpoint" key={method + path}>
                      <span className={'api-method method-' + method.toLowerCase()}>{method}</span>
                      <code>{path}</code>
                      <span className="api-description">{description}</span>
                    </div>
                  ))}
                </div>
              </article>
            ))}
          </div>

          <div className="api-footer-note">
            <span className="live-pulse" />
            Real-time task updates are also available through WebSocket connections.
            <code>/api/ws/tasks/{'{task_id}'}</code>
          </div>
        </section>

        {/* ── CTA ─────────────────────────────────────── */}
        <section className={`landing-cta${ctaVis ? ' reveal-visible' : ''}`} ref={ctaRef}>
          <div className="cta-glow" />
          <span className="landing-kicker reveal-fade-up"><Zap size={13} /> MAKE THE WEB WORK FOR YOU</span>
          <h2 className="reveal-fade-up">Your next workflow<br />starts with <em>one sentence.</em></h2>
          <p className="reveal-fade-up">Skip the repetitive clicks. Tell your agent what you need.</p>
          <button className="landing-primary reveal-fade-up" onClick={onOpen}>
            Open AI Browser Agent <ChevronRight size={17} />
          </button>
          <div className="cta-orbit cta-orbit-one" />
          <div className="cta-orbit cta-orbit-two" />
        </section>

      </main>

      <footer className="landing-footer">
        <a className="landing-brand" href="#top">
          <span className="landing-brand-mark"><Command size={16} /></span>
          <span>browser<span className="brand-accent">agent</span></span>
        </a>
        <span>Automation with visibility. Intelligence with control.</span>
        <a href="#top">Back to top ↑</a>
      </footer>
    </div>
  );
}
