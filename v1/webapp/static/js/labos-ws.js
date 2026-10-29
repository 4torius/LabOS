/*!
 * labos-ws.js — one reconnecting WebSocket, with subscriptions.
 *
 * The socket lived in base.html behind a single 40-line if/else chain over
 * `data.type`, which every page shared whether it cared or not. A page that
 * wanted to react to something had two options: add another branch to that
 * chain for everybody, or define a globally-named function the chain happened
 * to call by `typeof x === 'function'` (`log`, `refreshMobileTasks`). Both
 * make it impossible to tell, from a page, what it is actually listening to.
 *
 *     LabOS.ws.on('workflow_progress', function (msg) { ... });
 *     LabOS.ws.on('*', function (msg) { ... });          // everything
 *
 * Handlers are isolated: one that throws does not stop the others from
 * running, because a page's rendering bug must not silently take out the
 * status bar that tells an operator a run is in trouble.
 */
(function (global) {
    'use strict';

    var LabOS = global.LabOS = global.LabOS || {};

    var socket = null;
    var handlers = {};          // type -> [fn]
    var reconnectDelay = 1000;
    var MAX_RECONNECT_DELAY = 15000;
    var connected = false;
    var heartbeat = null;

    function emit(type, message) {
        var listeners = (handlers[type] || []).concat(handlers['*'] || []);
        for (var i = 0; i < listeners.length; i++) {
            try {
                listeners[i](message);
            } catch (err) {
                // Never let one page's handler break the rest — the status bar
                // and the intervention modal are on this socket too.
                console.error('LabOS.ws handler for "' + type + '" failed:', err);
            }
        }
    }

    function connect() {
        var scheme = global.location.protocol === 'https:' ? 'wss:' : 'ws:';
        socket = new WebSocket(scheme + '//' + global.location.host + '/ws');

        socket.onopen = function () {
            connected = true;
            reconnectDelay = 1000;
            emit('_open', {});
            console.log('LabOS: websocket connected');
        };

        socket.onmessage = function (event) {
            if (event.data === 'pong' || event.data === 'ping') return;
            var message;
            try {
                message = JSON.parse(event.data);
            } catch (err) {
                console.warn('LabOS: non-JSON websocket message', event.data);
                return;
            }
            emit(message.type, message);
        };

        socket.onerror = function () {
            // onclose always follows; reconnecting is handled there so it is
            // not attempted twice.
        };

        socket.onclose = function () {
            connected = false;
            emit('_close', {});
            // Back off instead of hammering a server that is restarting.
            setTimeout(connect, reconnectDelay);
            reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
        };
    }

    LabOS.ws = {
        /** Subscribe to one message type, or '*' for all. Returns an unsubscribe. */
        on: function (type, handler) {
            (handlers[type] = handlers[type] || []).push(handler);
            return function off() {
                handlers[type] = (handlers[type] || []).filter(function (h) {
                    return h !== handler;
                });
            };
        },

        get connected() { return connected; },

        /** For tests and for pages that want to exercise their own handlers. */
        _emit: emit
    };

    connect();

    // Keep the connection alive through anything that idles it out. The old
    // version called ws.send() unguarded on a timer, which threw every 30
    // seconds for as long as the socket was down.
    heartbeat = setInterval(function () {
        if (socket && socket.readyState === WebSocket.OPEN) {
            try { socket.send('ping'); } catch (err) { /* closing; onclose reconnects */ }
        }
    }, 30000);

    global.addEventListener('beforeunload', function () {
        clearInterval(heartbeat);
        if (socket) { socket.onclose = null; socket.close(); }
    });

})(window);
