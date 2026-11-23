// Abort, pause and resume of a running measurement.
//
// What i-control itself does, read out of its assemblies (i-control 2.0):
//
//   Stop      ConnectReader.StopScript() calls MeasurementServer.Stop(), which
//             aborts the SDK's "Measurement Thread" and waits for it (up to two
//             minutes). The SDK's handler for that abort sends the instrument
//             its abort sequence and re-initialises it.
//
//   Pause     MeasurementServer.Break() and Continue() exist and throw
//   Continue  NotSupportedException ("has been deprecated"). The Pause button
//             works another way: between two cycles of a KINETIC measurement
//             the SDK asks every registered ExternalActionListener whether to
//             carry on (a CustomAction whose data carries "KINETIC.PAUSE"), and
//             i-control's listener simply does not return while Pause is
//             pressed. Returning false instead aborts the run.
//
// So a pause is a request that takes effect at the next kinetic cycle boundary,
// and a single (non-kinetic) read cannot be paused at all, only aborted. That
// is the instrument's behaviour, not a limitation added here.
//
// Also here, because an abort cannot be reported without it: MeasurementServer
// .Run() does not throw when the measurement fails. It catches the exception,
// notifies its ExceptionListeners and returns normally. The bridge registered
// no listener, so a run that died half way was reported as a success.

using System;
using System.Threading;
using Microsoft.Extensions.Logging;
using Tecan.At.Measurement.Server;

namespace TecanSiLA2Server.Instrument
{
    public partial class TecanBridge
    {
        /// <summary>Key the SDK puts in the action it raises between kinetic cycles.</summary>
        private const string KineticPauseKey = "KINETIC.PAUSE";

        private const string AbortedByOperator = "Measurement aborted by operator";

        // Written by a gRPC thread, read by the SDK's measurement thread.
        private volatile bool _measurementActive;   // a worker is between start and finish
        private volatile bool _runStarted;          // MeasurementServer.Run() has been entered
        private volatile bool _pauseRequested;
        private volatile bool _abortRequested;
        private volatile bool _paused;              // actually waiting inside the SDK callback
        private volatile bool _abortedRun;          // the abort did interrupt this run
        private volatile Exception? _runException;  // what the SDK reported for this run

        /// <summary>True while the reader is held between two kinetic cycles.</summary>
        public bool IsPaused => _paused;

        /// <summary>
        /// Stop the running measurement now, the way i-control's Stop button does.
        /// Blocks until the SDK's measurement thread has ended.
        ///
        /// Success says the reader is not measuring when this returns; Aborted
        /// says a measurement was actually interrupted. Asking an idle reader
        /// to abort succeeds: an emergency stop must be able to read "stopped"
        /// off an instrument that had nothing to stop.
        /// </summary>
        public (bool Success, bool Aborted, string Message) AbortMeasurement()
        {
            if (!_measurementActive)
            {
                return (true, false, "No measurement was running");
            }

            _logger.LogWarning("[TecanBridge] AbortMeasurement requested");

            // First, so that a run waiting in a pause is released and so that
            // the worker knows how to report whatever happens next.
            _abortRequested = true;
            _pauseRequested = false;

            var server = _server;
            if (server == null)
            {
                return (false, false, "Not connected to instrument");
            }

            try
            {
                // Stop() does nothing if the SDK has not created its thread
                // yet, and the worker may be anywhere between loading the
                // protocol and entering Run(). Ask again until the worker is
                // gone: before Run() it sees _abortRequested and never starts;
                // inside Run() the next Stop() ends it.
                var deadline = DateTime.UtcNow.AddSeconds(150);
                while (_measurementActive && DateTime.UtcNow < deadline)
                {
                    if (_runStarted)
                    {
                        server.Stop();
                    }
                    if (_measurementActive)
                    {
                        Thread.Sleep(100);
                    }
                }
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "[TecanBridge] MeasurementServer.Stop() failed");
                return (false, false, "Abort failed: " + ex.Message);
            }

            if (_measurementActive)
            {
                return (false, false,
                    "The measurement did not stop within 150 s. Switch the reader off.");
            }
            return _abortedRun
                ? (true, true, "Measurement aborted")
                : (true, false, "The measurement had already finished; nothing was aborted");
        }

        /// <summary>
        /// Ask the running measurement to pause. Takes effect at the next
        /// kinetic cycle boundary; has no effect on a non-kinetic read.
        /// </summary>
        public (bool Success, string Message) PauseMeasurement()
        {
            if (!_measurementActive)
            {
                return (false, "No measurement is running");
            }
            if (_abortRequested)
            {
                return (false, "The measurement is being aborted");
            }
            _pauseRequested = true;
            _logger.LogInformation("[TecanBridge] Pause requested");
            return (true, _paused
                ? "Measurement is paused"
                : "Pause requested. The reader pauses between two kinetic cycles; " +
                  "a measurement without kinetic cycles cannot be paused, only aborted.");
        }

        /// <summary>Let a paused measurement continue, or withdraw a pending pause.</summary>
        public (bool Success, string Message) ResumeMeasurement()
        {
            if (!_pauseRequested)
            {
                return (false, "The measurement is not paused");
            }
            bool wasPaused = _paused;
            _pauseRequested = false;
            _logger.LogInformation("[TecanBridge] Resume requested (was paused: {Paused})", wasPaused);
            return (true, wasPaused ? "Measurement resumed" : "Pause request withdrawn");
        }

        // ── Called by the measurement worker ────────────────────────────────

        private void BeginRunControl()
        {
            _pauseRequested = false;
            _abortRequested = false;
            _paused = false;
            _runStarted = false;
            _abortedRun = false;
            _runException = null;
            _measurementActive = true;
        }

        private void EndRunControl()
        {
            _measurementActive = false;
            _runStarted = false;
            _paused = false;
            _pauseRequested = false;
        }

        /// <summary>
        /// MeasurementServer.Run(), with the listeners that make it possible
        /// to pause it, abort it, and know afterwards whether it worked.
        /// Throws OperationCanceledException if it was aborted and the SDK's
        /// own exception if the measurement failed.
        /// </summary>
        private void RunWithControl(Guid runGuid)
        {
            var server = _server!;
            var onExternalAction = new ExternalActionListener(OnExternalAction);
            var onException = new Tecan.At.Common.ExceptionListener(OnRunException);

            server.AddExternalActionListener(onExternalAction);
            server.AddExceptionListener(onException);
            try
            {
                if (_abortRequested)
                {
                    _abortedRun = true;
                    throw new OperationCanceledException(AbortedByOperator);
                }
                _runStarted = true;
                server.NewRunState(runGuid);
                server.Run(runGuid);
            }
            finally
            {
                _runStarted = false;
                _paused = false;
                server.RemoveExternalActionListener(onExternalAction);
                server.RemoveExceptionListener(onException);
            }

            // Run() returns normally whether the measurement finished, failed
            // or was stopped. Tell them apart here.
            if (_abortRequested)
            {
                _abortedRun = true;
                throw new OperationCanceledException(AbortedByOperator);
            }
            var failure = _runException;
            if (failure != null)
            {
                throw new Exception("Measurement failed: " + failure.Message, failure);
            }
        }

        // ── Called by the SDK, on its measurement thread ────────────────────

        private void OnRunException(object sender, Exception e)
        {
            // Keep the first one: what follows is usually a consequence of it.
            if (_runException == null)
            {
                _runException = e;
            }
            if (!_abortRequested)
            {
                _logger.LogError(e, "[TecanBridge] The SDK reported an error during the measurement");
            }
        }

        /// <summary>
        /// The SDK asks, between two kinetic cycles, whether to carry on.
        /// Returning true continues; returning false aborts the run. Not
        /// returning is the pause.
        /// </summary>
        private bool OnExternalAction(IExternalRuntimeState state)
        {
            var action = state.Action;
            bool betweenKineticCycles =
                action != null &&
                action.Data != null &&
                action.Data.Data != null &&
                action.Data.Data.ContainsKey(KineticPauseKey);

            // Any other action (a user request written into the protocol) is
            // left exactly as it was before this listener existed.
            if (!betweenKineticCycles)
            {
                return true;
            }

            if (_abortRequested)
            {
                state.ReturnObject = AbortedByOperator;
                return false;
            }
            if (!_pauseRequested)
            {
                return true;
            }

            _paused = true;
            UpdateStatus("Paused", "Measurement paused between kinetic cycles");
            while (_pauseRequested && !_abortRequested)
            {
                Thread.Sleep(100);
            }
            _paused = false;

            if (_abortRequested)
            {
                state.ReturnObject = AbortedByOperator;
                return false;
            }
            UpdateStatus("Busy", "Measurement resumed");
            return true;
        }
    }
}
