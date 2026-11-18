/* OpnDoor live camera player.
 *
 * OpnDoor relays a WebSocket to go2rtc, which sends fragmented MP4 that we feed to a
 * <video> through Media Source Extensions (ManagedMediaSource on iPhone/iPad).
 * When live video can't play, the caller's onfail shows refreshing stills instead.
 *
 *   const stop = OpnLive.play(videoEl, '/doors/ID/live', {onstate, onfail});
 *   stop();   // closes the stream
 */
(function () {
  const MS = window.ManagedMediaSource || window.MediaSource;
  const CODECS = ['avc1.640029', 'avc1.64002A', 'avc1.640033', 'avc1.4D401F', 'avc1.42E01E',
                  'hvc1.1.6.L153.B0', 'mp4a.40.2', 'mp4a.40.5', 'flac', 'opus'];

  function supported() {
    return !!MS && CODECS.some(c => MS.isTypeSupported('video/mp4; codecs="' + c + '"'));
  }

  function play(video, path, opts) {
    opts = opts || {};
    const say = s => opts.onstate && opts.onstate(s);
    let ws = null, closed = false, gotVideo = false, failTimer = null;
    const fail = why => { if (closed) return; stop(); opts.onfail && opts.onfail(why); };
    if (!supported()) { setTimeout(() => fail('This browser can\'t play live video.'), 0); return () => {}; }

    const ms = new MS();
    video.muted = true; video.playsInline = true; video.autoplay = true;
    if (window.ManagedMediaSource && MS === window.ManagedMediaSource) {
      video.disableRemotePlayback = true; video.srcObject = ms;
    } else {
      video.src = URL.createObjectURL(ms);
    }
    say('connecting');
    failTimer = setTimeout(() => { if (!gotVideo) fail('The camera didn\'t send any video.'); }, 15000);

    ms.addEventListener('sourceopen', () => {
      const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
      ws = new WebSocket(proto + location.host + path);
      ws.binaryType = 'arraybuffer';
      let sb = null; const queue = [];
      const pump = () => {
        if (!sb || sb.updating) return;
        if (queue.length) {
          try { sb.appendBuffer(queue.shift()); } catch (e) { queue.length = 0; }
          return;
        }
        // keep only the last few seconds so memory stays small on long views
        try {
          if (sb.buffered.length) {
            const end = sb.buffered.end(sb.buffered.length - 1), start = sb.buffered.start(0);
            if (end - start > 20) sb.remove(start, end - 10);
          }
        } catch (e) { /* ignore */ }
      };
      ws.onopen = () => {
        const list = CODECS.filter(c => MS.isTypeSupported('video/mp4; codecs="' + c + '"')).join(',');
        ws.send(JSON.stringify({type: 'mse', value: list}));
      };
      ws.onmessage = ev => {
        if (typeof ev.data === 'string') {
          let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
          if (msg.type === 'mse') {
            try { sb = ms.addSourceBuffer(msg.value); sb.mode = 'segments'; sb.addEventListener('updateend', pump); }
            catch (e) { fail('This camera\'s video format can\'t play in this browser.'); }
          } else if (msg.type === 'error') {
            fail(msg.value || 'Live video stopped.');
          } else if (msg.type === 'timeout') {
            closed = true; say('paused');
          }
          return;
        }
        if (!sb) return;
        queue.push(ev.data); pump();
        if (!gotVideo) { gotVideo = true; clearTimeout(failTimer); say('live'); }
        // stay at the live edge: if we fall behind (busy phone, slow network), jump ahead
        try {
          if (video.buffered.length) {
            const edge = video.buffered.end(video.buffered.length - 1);
            if (edge - video.currentTime > 2.5) video.currentTime = edge - 0.4;
          }
        } catch (e) { /* ignore */ }
        if (video.paused) video.play().catch(() => {});
      };
      ws.onclose = () => { if (!closed && !gotVideo) fail('Live video isn\'t available right now.'); else if (!closed) say('paused'); };
    }, {once: true});

    function stop() {
      closed = true; clearTimeout(failTimer);
      try { ws && ws.close(); } catch (e) { /* ignore */ }
      try { video.pause(); video.removeAttribute('src'); video.srcObject = null; video.load(); } catch (e) { /* ignore */ }
    }
    return stop;
  }

  /* Fallback: refresh a still picture every second. Returns a stop function. */
  function stills(img, url) {
    let on = true;
    const tick = () => {
      if (!on) return;
      if (!document.hidden) {
        const n = new Image();
        n.onload = () => { if (on) img.src = n.src; };
        n.src = url + (url.includes('?') ? '&' : '?') + 't=' + Date.now();
      }
      setTimeout(tick, 1000);
    };
    tick();
    return () => { on = false; };
  }

  window.OpnLive = {play, stills, supported};
})();
