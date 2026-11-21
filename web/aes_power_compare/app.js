const designs = {
  gcd: {
    order: '01', name: 'GCD', caseLabel: 'CASE 01 / SKY130HD', unit: 'µW', scale: 1e6,
    sinks: 35, clockPeriod: '20.0 ns', lecPoints: 52,
    setupBasePs: 12808.9530094, setupFinalPs: 13025.2519881,
    holdDeltaPs: -3.951561301,
    before: { buffers: 5, dummies: 3, inverters: 0 },
    after: { buffers: 0, dummies: 0, inverters: 5 },
    placement: 'xrow1006 · CTS-oracle winner · +1.071% signal HPWL',
    candidate: 'gcd_topology_safe_20261103 · xrow1006',
    summary: 'A five-buffer clock star became five polarity-safe inverters, and three balancing loads were pruned.',
    workloads: {
      idle: { baseline: 0.00013563292304755, optimized: 0.00011118625116069 },
      typical: { baseline: 0.00015076911222422, optimized: 0.00012608604811248998 },
      peak: { baseline: 0.00024004105944181998, optimized: 0.00021372755145418 }
    }
  },
  aes: {
    order: '02', name: 'AES', caseLabel: 'CASE 02 / SKY130HD', unit: 'mW', scale: 1e3,
    sinks: 562, clockPeriod: '3.8 ns', lecPoints: 562,
    setupBasePs: -41.9473344948, setupFinalPs: 1.89137594475,
    holdDeltaPs: -1.1745049369999983,
    before: { buffers: 109, dummies: 97, inverters: 0 },
    after: { buffers: 8, dummies: 0, inverters: 100 },
    placement: 'xrow39070 · CTS-aware placement selection · AES NDR route',
    candidate: 'topology_safe_graphfilter_aesndr_margin50_20261103',
    summary: 'The buffer-heavy tree was restructured with 100 inverters and eight buffers; all 97 baseline dummy loads were removed.',
    workloads: {
      idle: { baseline: 0.016572699882089, optimized: 0.010319220367819 },
      typical: { baseline: 0.07575605437159999, optimized: 0.06970009393990001 },
      peak: { baseline: 0.0771465562284, optimized: 0.07111771963537 }
    }
  },
  riscv32i: {
    order: '03', name: 'RISCV32I', caseLabel: 'CASE 03 / SKY130HD', unit: 'mW', scale: 1e3,
    sinks: 1056, clockPeriod: '7.0 ns', lecPoints: 1123,
    setupBasePs: 15.2300394518, setupFinalPs: 11.6742171485,
    holdDeltaPs: 14.051260156,
    before: { buffers: 112, dummies: 94, inverters: 0 },
    after: { buffers: 1, dummies: 0, inverters: 111 },
    placement: 'Two legal CTS-aware placement moves · timing-floor Stage 3',
    candidate: 'topology_safe_graphfilter_20261103 · 7.0 ns timing-floor run',
    summary: 'The clock tree went from 112 buffers and 94 dummy loads to 111 inverters and one remaining buffer.',
    workloads: {
      idle: { baseline: 0.012856853194533, optimized: 0.00960203143768 },
      typical: { baseline: 0.018474694807086, optimized: 0.015214225742966 },
      peak: { baseline: 0.023911121301348, optimized: 0.020658751949669 }
    }
  }
};

const workloadInfo = {
  idle: { label: 'Idle', note: 'low activity', glyph: '◌' },
  typical: { label: 'Typical', note: 'representative activity', glyph: '⌁' },
  peak: { label: 'Peak', note: 'high activity', glyph: '↗' }
};

const traceVisual = `
  <div class="detail-visual activity-visual" aria-hidden="true">
    <div class="wave-label">ACTIVITY TRACE <span>VCD</span></div>
    <svg viewBox="0 0 560 78" preserveAspectRatio="none">
      <path class="wave-grid" d="M0 20H560M0 40H560M0 60H560M70 0V78M140 0V78M210 0V78M280 0V78M350 0V78M420 0V78M490 0V78"/>
      <path class="wave-line wave-a" d="M0 48H22V24H38V48H64V48H78V17H95V48H116V48H129V31H145V48H170V48H182V20H200V48H226V48H244V36H256V48H281V48H294V15H312V48H332V48H348V28H366V48H389V48H404V17H421V48H446V48H458V34H473V48H497V48H509V21H528V48H560"/>
      <path class="wave-line wave-b" d="M0 66H45V54H58V66H105V66H118V58H130V66H168V66H181V52H195V66H233V66H247V57H258V66H296V66H309V50H323V66H363V66H377V60H388V66H426V66H439V53H453V66H492V66H505V56H520V66H560"/>
    </svg>
    <div class="wave-foot"><span>idle</span><span>typical</span><span>peak</span></div>
  </div>`;

let selectedDesign = 'aes';
let selectedWorkload = 'typical';
let selectedStep = 0;

function metrics(design, workload) {
  const values = design.workloads[workload];
  const saved = values.baseline - values.optimized;
  return { ...values, saved, reduction: saved / values.baseline * 100 };
}

function formatPower(value, design) {
  return (value * design.scale).toFixed(3);
}

function signed(value, digits = 2) {
  return `${value < 0 ? '−' : '+'}${Math.abs(value).toFixed(digits)}`;
}

function formatSlack(ps) {
  if (Math.abs(ps) >= 1000) return `${signed(ps / 1000, 3)} ns`;
  return `${signed(ps, 3)} ps`;
}

function renderOverview() {
  const container = document.getElementById('benchmarks');
  container.innerHTML = Object.entries(designs).map(([key, design]) => {
    const typical = metrics(design, 'typical');
    return `<button class="benchmark-card${key === selectedDesign ? ' selected' : ''}" type="button" role="tab" aria-selected="${key === selectedDesign}" data-design="${key}">
      <span class="benchmark-card-top"><span>${design.order} / SKY130HD</span><i>✓</i></span>
      <span class="benchmark-name">${design.name}</span>
      <span class="benchmark-main"><b>${typical.reduction.toFixed(2)}%</b><small>typical dynamic power saved</small></span>
      <span class="benchmark-workloads"><span>Idle <b>−${metrics(design, 'idle').reduction.toFixed(2)}%</b></span><span>Peak <b>−${metrics(design, 'peak').reduction.toFixed(2)}%</b></span></span>
      <span class="benchmark-foot"><span>${design.sinks.toLocaleString()} clock sinks · ${design.clockPeriod}</span><b>Sign-off PASS</b></span>
    </button>`;
  }).join('');
  container.querySelectorAll('[data-design]').forEach((button) => {
    button.addEventListener('click', () => setDesign(button.dataset.design));
    button.addEventListener('keydown', (event) => {
      if (!['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp'].includes(event.key)) return;
      event.preventDefault();
      const keys = Object.keys(designs);
      const current = keys.indexOf(button.dataset.design);
      const direction = ['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : -1;
      const next = keys[(current + direction + keys.length) % keys.length];
      container.querySelector(`[data-design="${next}"]`).focus();
      setDesign(next);
    });
  });
}

function updateHero(design) {
  const typical = metrics(design, 'typical');
  document.getElementById('heroCase').textContent = `${design.caseLabel}`;
  document.getElementById('heroPlatform').textContent = 'Sky130HD';
  document.getElementById('heroSinks').textContent = design.sinks.toLocaleString();
  document.getElementById('heroResultHead').textContent = `${design.name} · ROUTED TYPICAL`;
  document.getElementById('heroSaving').textContent = `−${formatPower(typical.saved, design)}`;
  document.getElementById('heroUnit').textContent = design.unit;
  document.getElementById('heroBaseline').textContent = `${formatPower(typical.baseline, design)} ${design.unit} baseline`;
  document.getElementById('heroPercent').textContent = `−${typical.reduction.toFixed(2)}%`;
  document.getElementById('heroMeter').style.width = `${Math.max(0, 100 - typical.reduction)}%`;
  document.getElementById('heroClockChange').textContent = `${design.before.buffers} → ${design.after.buffers} buffers`;
  document.getElementById('heroClockCaption').textContent = `${design.before.dummies} → ${design.after.dummies} dummy loads`;
}

function updateWorkloads(design) {
  document.querySelectorAll('.workload-card').forEach((card) => {
    const key = card.dataset.workload;
    const value = metrics(design, key);
    card.querySelector('.saving-value').textContent = `−${formatPower(value.saved, design)}`;
    card.querySelector('.saving-unit').textContent = design.unit;
    card.querySelector('.workload-percent').textContent = `${value.reduction.toFixed(2)}% less dynamic power`;
    card.querySelector('.baseline-value').textContent = formatPower(value.baseline, design);
    card.querySelector('.optimized-value').textContent = formatPower(value.optimized, design);
    card.querySelector('.power-unit').textContent = design.unit;
    card.querySelector('.card-line i').style.width = `${Math.min(100, value.reduction)}%`;
  });

  document.querySelectorAll('.power-row').forEach((row) => {
    const key = row.dataset.workload;
    const value = metrics(design, key);
    const ratio = value.optimized / value.baseline * 100;
    row.querySelector('.optimized-bar').style.width = `${ratio}%`;
    row.querySelector('.bar-row-value b').textContent = `−${value.reduction.toFixed(2)}%`;
    row.querySelector('.bar-row-value small').textContent = `−${formatPower(value.saved, design)} ${design.unit}`;
    row.setAttribute('aria-label', `${workloadInfo[key].label}: baseline ${formatPower(value.baseline, design)} ${design.unit}, optimized ${formatPower(value.optimized, design)} ${design.unit}`);
  });

  const idle = metrics(design, 'idle');
  const typical = metrics(design, 'typical');
  const peak = metrics(design, 'peak');
  document.getElementById('resultInsight').innerHTML = `<b>What stands out:</b> ${design.name} saves ${typical.reduction.toFixed(2)}% in typical activity, with reductions across idle (${idle.reduction.toFixed(2)}%) and peak (${peak.reduction.toFixed(2)}%) too.`;
  document.getElementById('resultsIntro').textContent = `${design.name} routed dynamic power: cell internal plus net switching, compared against its matched baseline using idle, typical, and peak gate-level VCDs.`;
  document.getElementById('selectedWorkload').textContent = `Selected · ${workloadInfo[selectedWorkload].label}`;
}

function updateClockTree(design) {
  document.getElementById('clockDescription').textContent = design.summary;
  document.getElementById('clockSaving').textContent = `${design.before.buffers} → ${design.after.buffers}`;
  document.getElementById('clockSavingLabel').innerHTML = `clock buffers before → after<br /><small>${design.before.dummies} → ${design.after.dummies} dummy loads · ${design.after.inverters} final inverters</small>`;
  document.getElementById('beforeBuffers').textContent = design.before.buffers;
  document.getElementById('beforeDummies').textContent = design.before.dummies;
  document.getElementById('beforeInverters').textContent = design.before.inverters;
  document.getElementById('afterBuffers').textContent = design.after.buffers;
  document.getElementById('afterDummies').textContent = design.after.dummies;
  document.getElementById('afterInverters').textContent = design.after.inverters;
}

function updateSignoff(design) {
  const setupDelta = design.setupFinalPs - design.setupBasePs;
  document.getElementById('finalWns').textContent = formatSlack(design.setupFinalPs).replace(/ (ns|ps)$/, '');
  document.getElementById('wnsUnit').textContent = Math.abs(design.setupFinalPs) >= 1000 ? 'ns' : 'ps';
  document.getElementById('baselineWns').textContent = formatSlack(design.setupBasePs);
  document.getElementById('setupDelta').textContent = `${signed(setupDelta, 2)} ps change`;
  document.getElementById('holdDelta').textContent = signed(design.holdDeltaPs, 3);
  document.getElementById('lecPoints').textContent = design.lecPoints.toLocaleString();
  document.getElementById('candidateName').textContent = design.candidate;
  document.getElementById('candidatePlatform').textContent = `${design.name} · SKY130HD`;
  document.getElementById('signoffTarget').textContent = `${design.clockPeriod} SDC`;
}

function flowStepsFor(design) {
  const typical = metrics(design, 'typical');
  const clockVisual = `<div class="detail-visual mini-tree-visual" aria-hidden="true"><div class="mini-tree-side"><span>BEFORE</span><b>${design.before.buffers} BUF</b><i class="mini-bar before-buf"></i><b>${design.before.dummies} DUMMY</b><i class="mini-bar before-dummy"></i></div><div class="mini-tree-arrow">→</div><div class="mini-tree-side after-mini"><span>FINAL</span><b>${design.after.buffers} BUF · ${design.after.dummies} DUMMY</b><i class="mini-bar after-buf"></i><b>${design.after.inverters} INV</b><i class="mini-bar after-inv"></i></div><small class="mini-tree-caption">Final routed clock-cell counts · ${design.sinks.toLocaleString()} sinks</small></div>`;
  const placementVisual = `<div class="detail-visual placement-visual" aria-hidden="true"><div class="placement-label">LEGAL CANDIDATES <span>TRIAL CTS SCORE</span></div><div class="placement-map"><i class="row-line"></i><i class="row-line"></i><i class="row-line"></i><i class="cell-dot c1"></i><i class="cell-dot c2"></i><i class="cell-dot c3"></i><i class="cell-dot c4"></i><i class="cell-dot c5"></i><i class="cell-dot chosen"></i><span class="placement-callout">${design.name}<b>SELECTED</b></span></div><div class="placement-foot"><span>legal placement</span><b>total clock-cell power</b><span>timing guard</span></div></div>`;
  return [
    {
      kicker: 'STEP 01 · INPUT SIGNAL', title: 'Use measured switching activity.',
      lede: `Matched gate-level VCD traces describe ${design.name}'s idle, typical, and peak workloads.`,
      what: 'The typical VCD guides activity-aware placement scoring. The final routed design is then measured again against all three workload traces.',
      why: 'The same physical change can affect idle and active workloads differently. Measuring each workload shows whether the result generalizes across activity levels.',
      proof: '0 unannotated VCD pins · idle, typical, and peak', badge: 'VCD COVERAGE', visual: traceVisual
    },
    {
      kicker: 'STEP 02 · CTS-AWARE PLACEMENT', title: 'Choose placement with the clock tree in view.',
      lede: `Each legal candidate is tested against a fast CTS build for ${design.sinks.toLocaleString()} clock sinks.`,
      what: `${design.placement}. The oracle compares total clock-cell internal power, while signal HPWL and setup/hold limits bound the move.`,
      why: 'The move is judged by the actual CTS cell mix and clock topology. That captures discrete dummy and buffer changes that HPWL or a flat wire estimate cannot reliably predict.',
      proof: `${design.sinks.toLocaleString()} clock sinks · ${design.clockPeriod} constraint · timing guard active`, badge: 'CTS ORACLE', visual: placementVisual
    },
    {
      kicker: 'STEP 03 · CLOCK RESTRUCTURING', title: 'Reshape the clock distribution.',
      lede: 'Only traced, parity-checked buffer branches are eligible for a cell substitution.',
      what: design.summary,
      why: 'Clock cells toggle every cycle. Their internal power and sink loading can dominate savings even when datapath activity is low. This is a clock-cell topology change, not a low-Vt cell swap.',
      proof: `${design.before.buffers} → ${design.after.buffers} buffers · ${design.before.dummies} → ${design.after.dummies} dummy loads · sink polarity preserved`, badge: 'GRAPH-CHECKED CTS', visual: clockVisual
    },
    {
      kicker: 'STEP 04 · ROUTE & SIGN-OFF', title: 'Keep only the routed, verified candidate.',
      lede: `Detailed routing, extracted SPEF, gate-level VCD measurement, timing, and formal checks were completed for ${design.name}.`,
      what: `Typical dynamic power changed by −${formatPower(typical.saved, design)} ${design.unit}. Final setup WNS is ${formatSlack(design.setupFinalPs)}; the setup change is ${signed(design.setupFinalPs - design.setupBasePs, 2)} ps.`,
      why: 'Power savings count only after routed SPEF measurement, with timing, hold, route DRC/antenna, VDD/VSS connectivity, complete VCD activity, and formal equivalence checked.',
      proof: `DRC 0 · antenna 0 · VDD/VSS pass · LEC ${design.lecPoints.toLocaleString()}/${design.lecPoints.toLocaleString()} · VCD drops 0`, badge: 'FINAL GATES',
      visual: `<div class="detail-visual gate-visual" aria-hidden="true"><span><i>✓</i> TIMING</span><span><i>✓</i> ROUTE</span><span><i>✓</i> POWER GRID</span><span><i>✓</i> LOGIC</span><span><i>✓</i> VCD</span></div>`
    }
  ];
}

function setWorkload(key) {
  if (!workloadInfo[key]) return;
  selectedWorkload = key;
  document.querySelectorAll('[data-workload]').forEach((element) => {
    const active = element.dataset.workload === key;
    if (element.classList.contains('workload-card') || element.classList.contains('power-row')) {
      element.classList.toggle('selected', active);
      element.setAttribute('aria-selected', String(active));
      element.tabIndex = active ? 0 : -1;
    }
  });
  document.getElementById('selectedWorkload').textContent = `Selected · ${workloadInfo[key].label}`;
}

function setStep(index) {
  const steps = flowStepsFor(designs[selectedDesign]);
  const step = steps[index];
  if (!step) return;
  selectedStep = index;
  document.querySelectorAll('.step-tab').forEach((button, i) => {
    const active = i === index;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
  });
  const rail = document.querySelector('.step-rail i');
  if (rail) rail.style.height = `${((index + 1) / steps.length) * 100}%`;
  const panel = document.getElementById('stepDetail');
  panel.style.animation = 'none';
  void panel.offsetHeight;
  panel.style.animation = '';
  panel.innerHTML = `
    <div class="step-detail-top"><span class="step-kicker">${step.kicker}</span><span class="step-ok"><i>✓</i> ${step.badge}</span></div>
    <h3>${step.title}</h3>
    <p class="step-lede">${step.lede}</p>
    <div class="detail-columns"><div><span class="detail-label">WHAT HAPPENS</span><p>${step.what}</p></div><div><span class="detail-label">WHY IT MATTERS</span><p>${step.why}</p></div></div>
    <div class="step-proof"><span class="proof-icon">✓</span><span><b>Verification point</b><small>${step.proof}</small></span><strong>PASS</strong></div>
    ${step.visual}`;
}

function setDesign(key) {
  if (!designs[key]) return;
  selectedDesign = key;
  const design = designs[key];
  renderOverview();
  updateHero(design);
  updateWorkloads(design);
  updateClockTree(design);
  updateSignoff(design);
  setWorkload(selectedWorkload);
  setStep(selectedStep);
}

document.querySelectorAll('[data-workload]').forEach((element) => {
  element.addEventListener('click', () => setWorkload(element.dataset.workload));
});
document.querySelectorAll('.step-tab').forEach((button) => {
  button.addEventListener('click', () => setStep(Number(button.dataset.step)));
  button.addEventListener('keydown', (event) => {
    if (!['ArrowDown', 'ArrowUp', 'ArrowRight', 'ArrowLeft'].includes(event.key)) return;
    event.preventDefault();
    const current = Number(button.dataset.step);
    const direction = ['ArrowDown', 'ArrowRight'].includes(event.key) ? 1 : -1;
    const next = (current + direction + 4) % 4;
    document.querySelector(`.step-tab[data-step="${next}"]`).focus();
    setStep(next);
  });
});

setDesign(selectedDesign);
setWorkload(selectedWorkload);
