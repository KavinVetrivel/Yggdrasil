const apiBase = '';

const apiStatus = document.getElementById('api-status');
const subjectForm = document.getElementById('subject-form');
const topicForm = document.getElementById('topic-form');
const resourceForm = document.getElementById('resource-form');
const pathForm = document.getElementById('path-form');
const subjectOutput = document.getElementById('subject-output');
const topicOutput = document.getElementById('topic-output');
const resourceOutput = document.getElementById('resource-output');
const pathOutput = document.getElementById('path-output');
const graphOutput = document.getElementById('graph-output');
const quickButtons = document.querySelectorAll('[data-load-subject], [data-load-topic]');

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function setStatus(kind, text) {
  apiStatus.className = `status-pill status-${kind}`;
  apiStatus.textContent = text;
}

async function fetchJson(path, options = {}) {
  const response = await fetch(`${apiBase}${path}`, {
    headers: { 'Accept': 'application/json' },
    ...options,
  });

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : `Request failed (${response.status})`;
    throw new Error(Array.isArray(detail) ? detail.join(', ') : detail);
  }

  return payload;
}

function renderEmpty(target, message) {
  target.classList.add('muted');
  target.innerHTML = `<p>${escapeHtml(message)}</p>`;
}

function renderSubjectTopics(data) {
  const subject = data.subject;
  const units = data.units || [];
  const badgeHtml = [
    `<span class="meta-badge">${escapeHtml(subject.code)}</span>`,
    `<span class="meta-badge">Semester ${escapeHtml(subject.semester)}</span>`,
    `<span class="meta-badge">${escapeHtml(subject.credits)} credits</span>`,
  ].join('');

  const unitsHtml = units.length
    ? `<div class="units-grid">${units.map((unit) => {
        const topics = (unit.topics || []).map((topic) => `<span class="topic-chip">${escapeHtml(topic)}</span>`).join('');
        return `
          <details class="unit-card">
            <summary>
              <span>Unit ${escapeHtml(unit.number)} · ${escapeHtml(unit.title)}</span>
              <span class="sem-badge">${(unit.topics || []).length} topics</span>
            </summary>
            <div class="topic-list">${topics || '<span class="topic-chip">No topics captured</span>'}</div>
          </details>
        `;
      }).join('')}</div>`
    : '<p>No unit entries were found for this subject.</p>';

  subjectOutput.classList.remove('muted');
  subjectOutput.innerHTML = `
    <div class="subject-title">
      <div>
        <h3>${escapeHtml(subject.name)}</h3>
        <div class="meta-row">${badgeHtml}</div>
      </div>
    </div>
    ${unitsHtml}
  `;
}

function renderRelatedTopics(data) {
  const matches = data.matches || [];
  if (!matches.length) {
    renderEmpty(topicOutput, 'No related topic matches were returned.');
    return;
  }

  topicOutput.classList.remove('muted');
  topicOutput.innerHTML = `
    <div class="cards-grid">
      ${matches.map((match) => {
        const topic = match.topic;
        const related = (match.related || []).map((item) => `
          <div class="item-card">
            <strong>${escapeHtml(item.name)}</strong>
            <p>${escapeHtml(item.subject_code)} · ${escapeHtml(item.unit_key)}</p>
          </div>
        `).join('');

        return `
          <div class="topic-match">
            <h3>${escapeHtml(topic.name)}</h3>
            <p>${escapeHtml(topic.subject_code)} · ${escapeHtml(topic.unit_key)}</p>
            <div class="cards-grid">
              ${related || '<p>No outgoing or incoming RELATED_TO links found.</p>'}
            </div>
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function renderResources(data) {
  const resources = data.resources || { textbooks: [], references: [] };
  const textbooks = (resources.textbooks || []).map((item) => `<div class="resource-item">${escapeHtml(item)}</div>`).join('');
  const references = (resources.references || []).map((item) => `<div class="resource-item">${escapeHtml(item)}</div>`).join('');

  resourceOutput.classList.remove('muted');
  resourceOutput.innerHTML = `
    <div class="resource-title">
      <h3>${escapeHtml(data.code)}</h3>
      <span class="meta-badge">${(resources.textbooks || []).length} textbooks · ${(resources.references || []).length} references</span>
    </div>
    <div class="resource-list">
      <strong>Textbooks</strong>
      ${textbooks || '<div class="resource-item">No textbook entries found.</div>'}
      <strong>References</strong>
      ${references || '<div class="resource-item">No reference entries found.</div>'}
    </div>
  `;
}

function renderPath(data) {
  const bySemester = data.by_semester || [];
  pathOutput.classList.remove('muted');
  pathOutput.innerHTML = `
    <div class="path-title">
      <div>
        <h3>${escapeHtml(data.from)} → ${escapeHtml(data.to)}</h3>
        <p>${(data.subjects || []).length} subjects across ${bySemester.length} semesters</p>
      </div>
      <span class="meta-badge">Topological order</span>
    </div>
    <div class="timeline-grid">
      ${bySemester.map((semesterBlock) => `
        <section class="timeline-card">
          <h3>Semester ${escapeHtml(semesterBlock.semester)}</h3>
          <div class="semester-list">
            ${(semesterBlock.subjects || []).map((subject) => `
              <span class="topic-chip">${escapeHtml(subject.code)} · ${escapeHtml(subject.name)}</span>
            `).join('')}
          </div>
        </section>
      `).join('')}
    </div>
  `;

  renderLearningPathGraph(data);
}

function renderLearningPathGraph(data) {
  const semesters = data.by_semester || [];
  const subjects = data.subjects || [];
  const edges = data.prerequisite_edges || [];

  if (!semesters.length || !subjects.length) {
    renderEmpty(graphOutput, 'No learning-path data is available to draw.');
    return;
  }

  const laneWidth = 260;
  const laneGap = 18;
  const headerHeight = 72;
  const nodeHeight = 76;
  const rowGap = 26;
  const laneInnerWidth = laneWidth - 34;
  const maxRows = Math.max(...semesters.map((semesterBlock) => (semesterBlock.subjects || []).length), 1);
  const graphWidth = semesters.length * laneWidth + (semesters.length - 1) * laneGap + 48;
  const graphHeight = headerHeight + maxRows * (nodeHeight + rowGap) + 56;

  const subjectIndex = new Map();
  const laneLookup = new Map();

  semesters.forEach((semesterBlock, laneIndex) => {
    const laneLeft = 24 + laneIndex * (laneWidth + laneGap);
    (semesterBlock.subjects || []).forEach((subject, rowIndex) => {
      const top = headerHeight + rowIndex * (nodeHeight + rowGap);
      const centerX = laneLeft + laneWidth / 2;
      const centerY = top + nodeHeight / 2;
      subjectIndex.set(subject.code, {
        ...subject,
        laneIndex,
        laneLeft,
        top,
        left: laneLeft + 18,
        width: laneInnerWidth,
        centerX,
        centerY,
      });
      laneLookup.set(subject.code, semesterBlock.semester);
    });
  });

  const svgEdges = edges
    .map((edge) => {
      const source = subjectIndex.get(edge.from_code);
      const target = subjectIndex.get(edge.to_code);
      if (!source || !target) {
        return '';
      }

      const midX = (source.centerX + target.centerX) / 2;
      const startX = source.centerX + 82;
      const startY = source.centerY;
      const endX = target.centerX - 82;
      const endY = target.centerY;
      const bendColor = source.semester === target.semester ? 'rgba(255, 210, 122, 0.45)' : 'rgba(124, 229, 197, 0.45)';

      return `
        <path d="M ${startX} ${startY} C ${midX} ${startY}, ${midX} ${endY}, ${endX} ${endY}" stroke="${bendColor}" stroke-width="2.2" fill="none" marker-end="url(#graph-arrow)" />
      `;
    })
    .join('');

  const laneHtml = semesters.map((semesterBlock, laneIndex) => {
    const laneLeft = 24 + laneIndex * (laneWidth + laneGap);
    const nodes = (semesterBlock.subjects || []).map((subject, rowIndex) => {
      const top = headerHeight + rowIndex * (nodeHeight + rowGap);
      return `
        <div class="graph-node" style="top:${top}px;left:${laneLeft + 18}px;width:${laneInnerWidth}px;height:${nodeHeight}px;">
          <strong>${escapeHtml(subject.code)}</strong>
          <span>${escapeHtml(subject.name)}</span>
          <small>Semester ${escapeHtml(subject.semester)}</small>
        </div>
      `;
    }).join('');

    return `
      <section class="graph-lane">
        <h3>Semester ${escapeHtml(semesterBlock.semester)}</h3>
        ${nodes}
      </section>
    `;
  }).join('');

  graphOutput.classList.remove('muted');
  graphOutput.innerHTML = `
    <div class="graph-legend">
      <span class="legend-item"><span class="legend-swatch"></span>Prerequisite edges</span>
      <span class="legend-item">${subjects.length} nodes</span>
      <span class="legend-item">${edges.length} edges</span>
    </div>
    <div class="graph-viewport">
      <div class="graph-scene" style="width:${graphWidth}px;height:${graphHeight}px;">
        <svg class="graph-edge-layer" viewBox="0 0 ${graphWidth} ${graphHeight}" preserveAspectRatio="none" aria-hidden="true">
          <defs>
            <marker id="graph-arrow" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto" markerUnits="strokeWidth">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(124, 229, 197, 0.68)" />
            </marker>
          </defs>
          ${svgEdges}
        </svg>
        <div class="graph-lanes" style="--semester-count:${semesters.length};">
          ${laneHtml}
        </div>
      </div>
    </div>
  `;
}

async function loadSubjectTopics(code) {
  setStatus('waiting', `Loading ${code}`);
  renderEmpty(subjectOutput, 'Loading subject topics...');
  const data = await fetchJson(`/subject/${encodeURIComponent(code)}/topics`);
  renderSubjectTopics(data);
  setStatus('ok', 'API online');
}

async function loadTopicRelated(name) {
  setStatus('waiting', `Tracing ${name}`);
  renderEmpty(topicOutput, 'Loading related topics...');
  const data = await fetchJson(`/topic/${encodeURIComponent(name)}/related`);
  renderRelatedTopics(data);
  setStatus('ok', 'API online');
}

async function loadResources(code) {
  setStatus('waiting', `Reading ${code}`);
  renderEmpty(resourceOutput, 'Loading resources...');
  const data = await fetchJson(`/subject/${encodeURIComponent(code)}/resources`);
  renderResources(data);
  setStatus('ok', 'API online');
}

async function loadPath(fromSemester, toSemester) {
  setStatus('waiting', `Building ${fromSemester} → ${toSemester}`);
  renderEmpty(pathOutput, 'Building learning path...');
  renderEmpty(graphOutput, 'Building graph visualization...');
  const data = await fetchJson(`/path?from=${encodeURIComponent(fromSemester)}&to=${encodeURIComponent(toSemester)}`);
  renderPath(data);
  setStatus('ok', 'API online');
}

subjectForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const code = document.getElementById('subject-code').value.trim();
  if (!code) return;
  try {
    await loadSubjectTopics(code);
  } catch (error) {
    setStatus('bad', 'Query failed');
    renderEmpty(subjectOutput, error.message);
  }
});

topicForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const name = document.getElementById('topic-name').value.trim();
  if (!name) return;
  try {
    await loadTopicRelated(name);
  } catch (error) {
    setStatus('bad', 'Query failed');
    renderEmpty(topicOutput, error.message);
  }
});

resourceForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const code = document.getElementById('resource-code').value.trim();
  if (!code) return;
  try {
    await loadResources(code);
  } catch (error) {
    setStatus('bad', 'Query failed');
    renderEmpty(resourceOutput, error.message);
  }
});

pathForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const fromSemester = document.getElementById('path-from').value.trim();
  const toSemester = document.getElementById('path-to').value.trim();
  if (!fromSemester || !toSemester) return;
  try {
    await loadPath(fromSemester, toSemester);
  } catch (error) {
    setStatus('bad', 'Query failed');
    renderEmpty(pathOutput, error.message);
    renderEmpty(graphOutput, error.message);
  }
});

quickButtons.forEach((button) => {
  button.addEventListener('click', async () => {
    const subject = button.dataset.loadSubject;
    const topic = button.dataset.loadTopic;
    if (subject) {
      document.getElementById('subject-code').value = subject;
      document.getElementById('resource-code').value = subject;
      try {
        await loadSubjectTopics(subject);
      } catch (error) {
        setStatus('bad', 'Query failed');
        renderEmpty(subjectOutput, error.message);
      }
      try {
        await loadResources(subject);
      } catch (error) {
        setStatus('bad', 'Query failed');
        renderEmpty(resourceOutput, error.message);
      }
    }
    if (topic) {
      document.getElementById('topic-name').value = topic;
      try {
        await loadTopicRelated(topic);
      } catch (error) {
        setStatus('bad', 'Query failed');
        renderEmpty(topicOutput, error.message);
      }
    }
  });
});

async function bootstrap() {
  try {
    const response = await fetchJson('/health');
    if (response.status === 'ok') {
      setStatus('ok', 'API online');
    }
  } catch {
    setStatus('bad', 'API unavailable');
  }
}

bootstrap();
