/* ============================================================================
   Shared missed-points feedback component (UI overhaul session 3).
   Included by BOTH study_plan.html and quiz.html (<script src="/static/feedback.js">)
   so grading feedback looks and behaves identically across Study and Quiz —
   a single definition rather than copy-pasted markup in each page.

   window.renderMissedFeedback(container, data, opts)
     container : the element to render the feedback into
     data      : a grade result with { awarded, total, score_pct,
                 points: [{ awarded, point_text, mark_point_id, evidence }],
                 leitner_box, next_review }
     opts      : {
        onRetry  : fn | null,   // show a "Try again" button wired to this
        onNext   : fn | null,   // show a "Next" button wired to this
        retryLabel, nextLabel,  // optional button text overrides
        leitner  : bool         // show the "Moved to Box N" line (default true)
     }

   Renders: "N of M points" + a thin green progress bar; one row per mark point
   (green check + point text if awarded; red X + "Missed: <label>" + evidence if
   not); then the retry / next buttons. Field names map directly to the
   grade_answer result shape (points[].awarded / point_text / mark_point_id /
   evidence) — no guessing.
   ========================================================================== */
(function () {
  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }
  function shortLabel(pt) {
    const t = pt.point_text || pt.mark_point_id || '';
    return t.length > 90 ? t.slice(0, 90) + '…' : t;
  }

  window.renderMissedFeedback = function (container, data, opts) {
    opts = opts || {};
    const awarded = data.awarded != null ? data.awarded : 0;
    const total = data.total != null ? data.total : 0;
    const pct = total ? Math.round((100 * awarded) / total) : (data.score_pct || 0);
    const points = data.points || [];

    const rows = points.map((pt) => {
      if (pt.awarded) {
        return `<div class="fb-point awarded">
            <span class="fb-ico"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg></span>
            <div class="fb-text">${esc(pt.point_text || pt.mark_point_id || '')}</div>
          </div>`;
      }
      const ev = pt.evidence ? `<div class="fb-evidence">${esc(pt.evidence)}</div>` : '';
      return `<div class="fb-point missed">
          <span class="fb-ico"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></span>
          <div class="fb-text"><span class="fb-missed-label">Missed:</span> ${esc(shortLabel(pt))}${ev}</div>
        </div>`;
    }).join('');

    const leitner = (opts.leitner !== false && data.leitner_box)
      ? `<div class="fb-leitner">Moved to <strong>Box ${esc(data.leitner_box)}</strong> — next review <strong>${esc(data.next_review || 'tomorrow')}</strong></div>`
      : '';
    const retryBtn = opts.onRetry
      ? `<button type="button" class="fb-btn fb-retry" data-fb="retry">${esc(opts.retryLabel || 'Try again with this in mind')}</button>`
      : '';
    const nextBtn = opts.onNext
      ? `<button type="button" class="fb-btn fb-next" data-fb="next">${esc(opts.nextLabel || 'Next')}</button>`
      : '';

    container.innerHTML = `
      <div class="fb">
        <div class="fb-head">
          <span class="fb-count">${awarded} of ${total} points</span>
          <span class="fb-pct">${pct}%</span>
        </div>
        <div class="fb-bar"><div class="fb-bar-fill" style="width:${pct}%"></div></div>
        <div class="fb-points">${rows}</div>
        ${leitner}
        <div class="fb-actions">${retryBtn}${nextBtn}</div>
      </div>`;

    if (opts.onRetry) {
      const b = container.querySelector('[data-fb="retry"]');
      if (b) b.addEventListener('click', opts.onRetry);
    }
    if (opts.onNext) {
      const b = container.querySelector('[data-fb="next"]');
      if (b) b.addEventListener('click', opts.onNext);
    }
  };
})();
