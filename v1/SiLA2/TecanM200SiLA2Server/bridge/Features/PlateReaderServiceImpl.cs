// PlateReaderServiceImpl.cs
// Implementazione del servizio SiLA2 per il Tecan Plate Reader

using System;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Xml.Linq;
using Google.Protobuf;
using Google.Protobuf.WellKnownTypes;
using Grpc.Core;
using Microsoft.Extensions.Logging;
using TecanSiLA2Server.AnIML;
using TecanSiLA2Server.Instrument;

namespace TecanSiLA2Server.Features
{
    /// <summary>
    /// Implementazione SiLA2 del servizio PlateReaderService.
    /// Questa classe implementa tutti i comandi e le proprietà definite nel file .sila.xml
    /// </summary>
    public class PlateReaderServiceImpl : PlateReaderService.PlateReaderServiceBase
    {
        private readonly ILogger<PlateReaderServiceImpl> _logger;
        private readonly TecanBridge _tecanBridge;

        // Observable properties state (accessed via TecanBridge, kept for future use)
        #pragma warning disable CS0169, CS0414
        private bool _isConnected;
        private string _operationalStatus = "Disconnected";
        private double _currentTemperature = 0.0;
        #pragma warning restore CS0169, CS0414

        public PlateReaderServiceImpl(ILogger<PlateReaderServiceImpl> logger, TecanBridge tecanBridge)
        {
            _logger = logger;
            _tecanBridge = tecanBridge;
        }

        #region Properties (Unary/Observable)

        /// <summary>
        /// Property: IsConnected (Observable)
        /// </summary>
        public override Task<GetIsConnectedResponse> GetIsConnected(
            GetIsConnectedRequest request,
            ServerCallContext context)
        {
            return Task.FromResult(new GetIsConnectedResponse
            {
                Value = _tecanBridge.IsConnected
            });
        }

        /// <summary>
        /// Property: IsConnected - Subscribe (Observable streaming)
        /// </summary>
        public override async Task SubscribeIsConnected(
            SubscribeIsConnectedRequest request,
            IServerStreamWriter<GetIsConnectedResponse> responseStream,
            ServerCallContext context)
        {
            var lastValue = !_tecanBridge.IsConnected; // Force first update
            while (!context.CancellationToken.IsCancellationRequested)
            {
                var currentValue = _tecanBridge.IsConnected;
                if (currentValue != lastValue)
                {
                    await responseStream.WriteAsync(new GetIsConnectedResponse
                    {
                        Value = currentValue
                    });
                    lastValue = currentValue;
                }
                await Task.Delay(500, context.CancellationToken);
            }
        }

        /// <summary>
        /// Property: OperationalStatus (Observable)
        /// </summary>
        public override Task<GetOperationalStatusResponse> GetOperationalStatus(
            GetOperationalStatusRequest request,
            ServerCallContext context)
        {
            return Task.FromResult(new GetOperationalStatusResponse
            {
                Value = _tecanBridge.CurrentStatus
            });
        }

        /// <summary>
        /// Property: OperationalStatus - Subscribe (Observable streaming)
        /// </summary>
        public override async Task SubscribeOperationalStatus(
            SubscribeOperationalStatusRequest request,
            IServerStreamWriter<GetOperationalStatusResponse> responseStream,
            ServerCallContext context)
        {
            var lastValue = "";
            while (!context.CancellationToken.IsCancellationRequested)
            {
                var currentValue = _tecanBridge.CurrentStatus;
                if (currentValue != lastValue)
                {
                    await responseStream.WriteAsync(new GetOperationalStatusResponse
                    {
                        Value = currentValue
                    });
                    lastValue = currentValue;
                }
                await Task.Delay(500, context.CancellationToken);
            }
        }

        /// <summary>
        /// Property: CurrentTemperature (Observable)
        /// </summary>
        public override Task<GetCurrentTemperatureResponse> GetCurrentTemperature(
            GetCurrentTemperatureRequest request,
            ServerCallContext context)
        {
            var temp = _tecanBridge.GetCurrentTemperature();
            return Task.FromResult(new GetCurrentTemperatureResponse
            {
                Value = double.IsNaN(temp) ? 0 : temp
            });
        }

        /// <summary>
        /// Property: CurrentTemperature - Subscribe (Observable streaming)
        /// </summary>
        public override async Task SubscribeCurrentTemperature(
            SubscribeCurrentTemperatureRequest request,
            IServerStreamWriter<GetCurrentTemperatureResponse> responseStream,
            ServerCallContext context)
        {
            while (!context.CancellationToken.IsCancellationRequested)
            {
                var temp = _tecanBridge.GetCurrentTemperature();
                await responseStream.WriteAsync(new GetCurrentTemperatureResponse
                {
                    Value = double.IsNaN(temp) ? 0 : temp
                });
                await Task.Delay(1000, context.CancellationToken);
            }
        }

        /// <summary>
        /// Property: InstrumentInfo (Non-Observable)
        /// </summary>
        public override Task<GetInstrumentInfoResponse> GetInstrumentInfo(
            GetInstrumentInfoRequest request,
            ServerCallContext context)
        {
            var info = _tecanBridge.GetInstrumentInfo();
            return Task.FromResult(new GetInstrumentInfoResponse
            {
                Value = new InstrumentInfoStructure
                {
                    SerialNumber = info.SerialNumber,
                    FirmwareVersion = info.FirmwareVersion,
                    ProductName = info.ProductName,
                    IsSimulated = info.IsSimulated
                }
            });
        }

        #endregion

        #region Commands

        /// <summary>
        /// Command: Connect
        /// </summary>
        public override Task<ConnectResponse> Connect(
            ConnectRequest request,
            ServerCallContext context)
        {
            _logger.LogInformation("Connect command received with connectionString: {ConnectionString}", 
                request.ConnectionString);

            // Se non specificato, usa "usb" per connettersi SOLO a strumenti reali
            // Valori supportati: "", "usb", "sim", "usb_auto", "any"
            var connectionString = string.IsNullOrEmpty(request.ConnectionString)
                ? "usb"  // DEFAULT: solo USB reale, NO simulatori
                : request.ConnectionString;

            _logger.LogInformation("Effective connection string: {ConnectionString}", connectionString);

            bool success = _tecanBridge.Connect(connectionString);

            if (!success)
            {
                throw new RpcException(new Status(
                    StatusCode.Internal,
                    "ConnectionFailed: Unable to connect to the instrument. " +
                    "Check that the Tecan M200 Pro is powered ON and USB cable is connected."));
            }

            // Ottieni info sullo strumento connesso
            var info = _tecanBridge.GetInstrumentInfo();
            _logger.LogInformation("Connected to: {Product} (SN: {Serial}, Simulated: {Simulated})",
                info.ProductName, info.SerialNumber, info.IsSimulated);

            return Task.FromResult(new ConnectResponse { Success = true });
        }

        /// <summary>
        /// Command: Disconnect
        /// </summary>
        public override Task<DisconnectResponse> Disconnect(
            DisconnectRequest request,
            ServerCallContext context)
        {
            _logger.LogInformation("Disconnect command received");
            bool success = _tecanBridge.Disconnect();
            return Task.FromResult(new DisconnectResponse { Success = success });
        }

        /// <summary>
        /// Command: PlateIn
        /// </summary>
        public override Task<PlateInResponse> PlateIn(
            PlateInRequest request,
            ServerCallContext context)
        {
            _logger.LogInformation("PlateIn command received");

            if (!_tecanBridge.IsConnected)
            {
                throw new RpcException(new Status(
                    StatusCode.FailedPrecondition,
                    "NotConnected: Instrument is not connected"));
            }

            bool success = _tecanBridge.PlateIn();

            if (!success)
            {
                throw new RpcException(new Status(
                    StatusCode.Internal,
                    "MovementFailed: Plate movement failed"));
            }

            return Task.FromResult(new PlateInResponse { Success = true });
        }

        /// <summary>
        /// Command: PlateOut
        /// </summary>
        public override Task<PlateOutResponse> PlateOut(
            PlateOutRequest request,
            ServerCallContext context)
        {
            _logger.LogInformation("PlateOut command received");

            if (!_tecanBridge.IsConnected)
            {
                throw new RpcException(new Status(
                    StatusCode.FailedPrecondition,
                    "NotConnected: Instrument is not connected"));
            }

            bool success = _tecanBridge.PlateOut();

            if (!success)
            {
                throw new RpcException(new Status(
                    StatusCode.Internal,
                    "MovementFailed: Plate movement failed"));
            }

            return Task.FromResult(new PlateOutResponse { Success = true });
        }

        /// <summary>
        /// Command: SetTemperature
        /// </summary>
        public override Task<SetTemperatureResponse> SetTemperature(
            SetTemperatureRequest request,
            ServerCallContext context)
        {
            _logger.LogInformation("SetTemperature command received: {Temp}°C", request.TargetTemperature);

            if (!_tecanBridge.IsConnected)
            {
                throw new RpcException(new Status(
                    StatusCode.FailedPrecondition,
                    "NotConnected: Instrument is not connected"));
            }

            if (request.TargetTemperature < 4.0 || request.TargetTemperature > 45.0)
            {
                throw new RpcException(new Status(
                    StatusCode.InvalidArgument,
                    "TemperatureOutOfRange: Temperature must be between 4.0 and 45.0°C"));
            }

            bool success = _tecanBridge.SetTemperature(request.TargetTemperature);

            return Task.FromResult(new SetTemperatureResponse { Success = success });
        }

        /// <summary>
        /// Command: TurnOffTemperature
        /// </summary>
        public override Task<TurnOffTemperatureResponse> TurnOffTemperature(
            TurnOffTemperatureRequest request,
            ServerCallContext context)
        {
            _logger.LogInformation("TurnOffTemperature command received");
            bool success = _tecanBridge.TurnOffTemperature();
            return Task.FromResult(new TurnOffTemperatureResponse { Success = success });
        }

        /// <summary>
        /// Command: RunMeasurement (Observable - Long Running with streaming progress)
        /// </summary>
        public override async Task RunMeasurement(
            RunMeasurementRequest request,
            IServerStreamWriter<RunMeasurementResponse> responseStream,
            ServerCallContext context)
        {
            _logger.LogInformation("RunMeasurement command received: Protocol={Protocol}, PlateID={PlateId}",
                request.ProtocolFile, request.PlateId);

            if (!_tecanBridge.IsConnected)
            {
                throw new RpcException(new Status(
                    StatusCode.FailedPrecondition,
                    "NotConnected: Instrument is not connected"));
            }

            if (_tecanBridge.CurrentStatus == "Busy")
            {
                throw new RpcException(new Status(
                    StatusCode.Unavailable,
                    "InstrumentBusy: Instrument is currently running another measurement"));
            }

            // Verify protocol exists
            var protocols = _tecanBridge.ListProtocols();
            if (!protocols.Contains(request.ProtocolFile) && 
                !File.Exists(Path.Combine(_tecanBridge.ProtocolsPath, request.ProtocolFile)))
            {
                throw new RpcException(new Status(
                    StatusCode.NotFound,
                    $"ProtocolNotFound: Protocol '{request.ProtocolFile}' not found"));
            }

            // Progress reporter
            var progress = new Progress<MeasurementProgress>(async p =>
            {
                try
                {
                    // Send intermediate response
                    await responseStream.WriteAsync(new RunMeasurementResponse
                    {
                        IsIntermediate = true,
                        Progress = p.Percentage,
                        StatusMessage = p.Message
                    });
                }
                catch (Exception ex)
                {
                    _logger.LogWarning(ex, "Failed to send progress update");
                }
            });

            // Run measurement
            var result = await _tecanBridge.RunMeasurementAsync(
                request.ProtocolFile,
                request.PlateId,
                progress,
                context.CancellationToken);

            if (!result.Success)
            {
                throw new RpcException(new Status(
                    StatusCode.Internal,
                    $"MeasurementFailed: {result.ErrorMessage}"));
            }

            // Convert to AnIML
            byte[] animlBytes = Array.Empty<byte>();
            if (File.Exists(result.XmlFilePath))
            {
                try
                {
                    var converter = new TecanToAnIMLConverter(
                        request.SampleSetId, 
                        request.PlateId);
                    var animlDoc = converter.ConvertFromFile(result.XmlFilePath!);
                    
                    // Save AnIML file
                    animlDoc.SaveToFile(result.AnIMLFilePath!);
                    
                    // Get as bytes for response
                    animlBytes = animlDoc.ToByteArray();
                    
                    _logger.LogInformation("AnIML document created: {Path}", result.AnIMLFilePath);
                }
                catch (Exception ex)
                {
                    _logger.LogError(ex, "Failed to convert to AnIML");
                }
            }

            // Send final response
            await responseStream.WriteAsync(new RunMeasurementResponse
            {
                IsIntermediate = false,
                Progress = 100,
                StatusMessage = "Completed",
                MeasurementResult = new MeasurementResultStructure
                {
                    AnimlDocument = ByteString.CopyFrom(animlBytes),
                    AnimlFilePath = result.AnIMLFilePath ?? "",
                    ExcelFilePath = result.ExcelFilePath ?? "",
                    MeasurementType = result.MeasurementType
                }
            });
        }

        /// <summary>
        /// Command: GetAnIMLResult
        /// </summary>
        public override Task<GetAnIMLResultResponse> GetAnIMLResult(
            GetAnIMLResultRequest request,
            ServerCallContext context)
        {
            _logger.LogInformation("GetAnIMLResult command received for PlateID: {PlateId}", request.PlateId);

            var animlDir = Path.Combine(_tecanBridge.ResultsPath, "AnIML");
            var matchingFiles = Directory.GetFiles(animlDir, $"{request.PlateId}*.animl")
                .OrderByDescending(f => File.GetCreationTime(f))
                .ToList();

            if (matchingFiles.Count == 0)
            {
                throw new RpcException(new Status(
                    StatusCode.NotFound,
                    "ResultNotFound: No AnIML result found for the specified PlateID"));
            }

            var animlContent = File.ReadAllBytes(matchingFiles[0]);
            return Task.FromResult(new GetAnIMLResultResponse
            {
                AnimlDocument = ByteString.CopyFrom(animlContent)
            });
        }

        /// <summary>
        /// Command: ListProtocols
        /// </summary>
        public override Task<ListProtocolsResponse> ListProtocols(
            ListProtocolsRequest request,
            ServerCallContext context)
        {
            _logger.LogInformation("ListProtocols command received");

            var protocols = _tecanBridge.ListProtocols();
            var response = new ListProtocolsResponse();
            response.Protocols.AddRange(protocols);

            return Task.FromResult(response);
        }

        #endregion
    }
}
