// SiLA2CommonServiceImpl.cs
// Implementation of SiLA2Common interface for Plug & Play discovery
// Enables generic clients to discover and execute commands without specific stubs

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using Grpc.Core;
using Microsoft.Extensions.Logging;
using TecanSiLA2Server.AnIML;
using TecanSiLA2Server.Instrument;

namespace TecanSiLA2Server.Features
{
    /// <summary>
    /// SiLA2Common service implementation for Plug & Play support.
    /// Allows generic discovery and command execution.
    /// </summary>
    public class SiLA2CommonServiceImpl : SiLA2ServerInfo.SiLA2ServerInfoBase
    {
        private readonly ILogger<SiLA2CommonServiceImpl> _logger;
        private readonly PlateReaderServiceImpl _plateReaderService;
        private readonly TecanBridge _tecanBridge;
        private readonly ServerConfiguration _config;
        private readonly Stopwatch _uptimeWatch;

        public SiLA2CommonServiceImpl(
            ILogger<SiLA2CommonServiceImpl> logger,
            PlateReaderServiceImpl plateReaderService,
            TecanBridge tecanBridge,
            ServerConfiguration config)
        {
            _logger = logger;
            _plateReaderService = plateReaderService;
            _tecanBridge = tecanBridge;
            _config = config;
            _uptimeWatch = Stopwatch.StartNew();
        }

        /// <summary>
        /// Get server metadata for discovery.
        /// </summary>
        public override Task<ServerInfoResponse> GetServerInfo(
            GetServerInfoRequest request,
            ServerCallContext context)
        {
            _logger.LogInformation("[PnP] GetServerInfo called");

            var instrumentInfo = _tecanBridge.GetInstrumentInfo();

            var response = new ServerInfoResponse
            {
                ServerName = _config.ServerName,
                ServerType = "plate_reader",
                Vendor = "Tecan / BicoccaLab",
                Model = "Infinite M200 Pro",
                SerialNumber = instrumentInfo?.SerialNumber ?? "",
                ServerVersion = "1.0.0",
                SilaVersion = "2.0",
                Description = "SiLA2 Server for Tecan M200 Pro plate reader with AnIML integration",
                Host = "localhost",
                UptimeSeconds = (int)_uptimeWatch.Elapsed.TotalSeconds,
                HardwareConnected = _tecanBridge.IsConnected,
                HardwareStatus = _tecanBridge.CurrentStatus
            };

            response.Capabilities.AddRange(new[]
            {
                "absorbance",
                "fluorescence",
                "luminescence",
                "temperature_control",
                "plate_handling",
                "animl_export"
            });

            return Task.FromResult(response);
        }

        /// <summary>
        /// Get available features and their commands.
        /// </summary>
        public override Task<FeaturesResponse> GetFeatures(
            GetFeaturesRequest request,
            ServerCallContext context)
        {
            _logger.LogInformation("[PnP] GetFeatures called");

            var response = new FeaturesResponse();

            // PlateReaderService feature
            var plateReaderFeature = new Feature
            {
                Identifier = "PlateReaderService",
                DisplayName = "Plate Reader Service",
                Description = "Control Tecan M200 Pro plate reader",
                Category = "instruments.platereader",
                Version = "1.0"
            };

            // Add commands
            plateReaderFeature.Commands.AddRange(new[]
            {
                CreateCommand("Connect", "Connect", "Connect to the Tecan instrument"),
                CreateCommand("Disconnect", "Disconnect", "Disconnect from the instrument"),
                CreateCommand("PlateIn", "Plate In", "Move plate into the reader"),
                CreateCommand("PlateOut", "Plate Out", "Move plate out of the reader"),
                CreateCommand("SetTemperature", "Set Temperature", "Set plate temperature (4-45°C)",
                    new[] { CreateParameter("temperature", "double", "Target temperature in °C", true) }),
                CreateCommand("TurnOffTemperature", "Turn Off Temperature", "Turn off temperature control"),
                CreateCommand("RunMeasurement", "Run Measurement", "Execute a measurement protocol",
                    new[] { CreateParameter("protocol_file", "string", "Protocol filename (.mdfx)", true) },
                    isObservable: true),
                CreateCommand("ListProtocols", "List Protocols", "List available .mdfx protocols"),
                CreateCommand("GetAnIMLResult", "Get AnIML Result", "Retrieve results in AnIML format",
                    new[] { CreateParameter("plate_id", "string", "Plate identifier", true) })
            });

            // Add properties
            plateReaderFeature.Properties.AddRange(new[]
            {
                CreateProperty("IsConnected", "Is Connected", "Connection status", "bool", true),
                CreateProperty("OperationalStatus", "Operational Status", "Current operational state", "string", true),
                CreateProperty("CurrentTemperature", "Current Temperature", "Current plate temperature", "double", true),
                CreateProperty("InstrumentInfo", "Instrument Info", "Instrument details", "object", false)
            });

            response.Features.Add(plateReaderFeature);

            return Task.FromResult(response);
        }

        /// <summary>
        /// Get current server and hardware status.
        /// </summary>
        public override Task<StatusResponse> GetStatus(
            GetStatusRequest request,
            ServerCallContext context)
        {
            _logger.LogInformation("[PnP] GetStatus called");

            var instrumentInfo = _tecanBridge.GetInstrumentInfo();

            var response = new StatusResponse
            {
                Status = "running",
                ServerOnline = true,
                HardwareOnline = _tecanBridge.IsConnected,
                HardwareStatus = _tecanBridge.CurrentStatus,
                ErrorMessage = _tecanBridge.LastMessage ?? "",
                UptimeSeconds = (long)_uptimeWatch.Elapsed.TotalSeconds
            };

            response.Details["temperature"] = _tecanBridge.GetCurrentTemperature().ToString("F1");
            response.Details["serial"] = instrumentInfo?.SerialNumber ?? "";
            response.Details["simulated"] = (instrumentInfo?.IsSimulated ?? true).ToString();
            response.Details["is_busy"] = (_tecanBridge.CurrentStatus == "Busy").ToString();

            return Task.FromResult(response);
        }

        /// <summary>
        /// Execute any command generically.
        /// Routes to appropriate PlateReaderService method.
        /// </summary>
        public override async Task ExecuteCommand(
            ExecuteCommandRequest request,
            IServerStreamWriter<ExecuteCommandResponse> responseStream,
            ServerCallContext context)
        {
            var feature = request.Feature;
            var command = request.Command;
            var parameters = request.Parameters;
            var startTime = DateTime.Now;

            _logger.LogInformation("[PnP] ExecuteCommand START: {Feature}.{Command} params=[{Params}]", 
                feature, command, string.Join(", ", parameters.Keys));

            try
            {
                var result = await ExecuteCommandInternal(command, parameters, responseStream, context);
                
                var elapsed = (DateTime.Now - startTime).TotalSeconds;
                _logger.LogInformation("[PnP] ExecuteCommand COMPLETED: {Feature}.{Command} in {Elapsed:F1}s - Success=true, ResultKeys=[{Keys}]", 
                    feature, command, elapsed, string.Join(", ", result.Keys));

                await responseStream.WriteAsync(new ExecuteCommandResponse
                {
                    Success = true,
                    Result = { result },
                    IsIntermediate = false,
                    Progress = 100
                });
            }
            catch (Exception ex)
            {
                var elapsed = (DateTime.Now - startTime).TotalSeconds;
                _logger.LogError(ex, "[PnP] ExecuteCommand FAILED: {Feature}.{Command} after {Elapsed:F1}s - {Error}", 
                    feature, command, elapsed, ex.Message);

                await responseStream.WriteAsync(new ExecuteCommandResponse
                {
                    Success = false,
                    Error = ex.Message,
                    IsIntermediate = false,
                    Progress = 0
                });
            }
        }

        /// <summary>
        /// Internal command routing.
        /// </summary>
        private async Task<Dictionary<string, string>> ExecuteCommandInternal(
            string command,
            IDictionary<string, string> parameters,
            IServerStreamWriter<ExecuteCommandResponse> responseStream,
            ServerCallContext context)
        {
            var result = new Dictionary<string, string>();

            switch (command)
            {
                case "Connect":
                    var connectResp = await _plateReaderService.Connect(new ConnectRequest(), context);
                    result["success"] = connectResp.Success.ToString();
                    break;

                case "Disconnect":
                    var disconnectResp = await _plateReaderService.Disconnect(new DisconnectRequest(), context);
                    result["success"] = disconnectResp.Success.ToString();
                    break;

                case "PlateIn":
                    var plateInResp = await _plateReaderService.PlateIn(new PlateInRequest(), context);
                    result["success"] = plateInResp.Success.ToString();
                    break;

                case "PlateOut":
                    var plateOutResp = await _plateReaderService.PlateOut(new PlateOutRequest(), context);
                    result["success"] = plateOutResp.Success.ToString();
                    break;

                case "SetTemperature":
                    // Accept multiple parameter name variants
                    string? tempStrValue = null;
                    if (parameters.TryGetValue("temperature", out var t1)) tempStrValue = t1;
                    else if (parameters.TryGetValue("Temperature", out var t2)) tempStrValue = t2;
                    else if (parameters.TryGetValue("TargetTemperature", out var t3)) tempStrValue = t3;
                    else if (parameters.TryGetValue("target_temperature", out var t4)) tempStrValue = t4;
                    
                    if (!string.IsNullOrEmpty(tempStrValue) && double.TryParse(tempStrValue, out var temp))
                    {
                        var setTempResp = await _plateReaderService.SetTemperature(
                            new SetTemperatureRequest { TargetTemperature = temp }, context);
                        result["success"] = setTempResp.Success.ToString();
                    }
                    else
                    {
                        var receivedParams = string.Join(", ", parameters.Keys);
                        throw new ArgumentException($"Missing or invalid temperature parameter. Received: [{receivedParams}]");
                    }
                    break;

                case "TurnOffTemperature":
                    var turnOffResp = await _plateReaderService.TurnOffTemperature(
                        new TurnOffTemperatureRequest(), context);
                    result["success"] = turnOffResp.Success.ToString();
                    break;

                case "RunMeasurement":
                    // Accept multiple parameter name variants for protocol file
                    string? protocolFile = null;
                    if (parameters.TryGetValue("protocol_file", out var pf1)) protocolFile = pf1;
                    else if (parameters.TryGetValue("ProtocolFile", out var pf2)) protocolFile = pf2;
                    else if (parameters.TryGetValue("ProtocolPath", out var pf3)) protocolFile = pf3;
                    else if (parameters.TryGetValue("protocol", out var pf4)) protocolFile = pf4;
                    else if (parameters.TryGetValue("Protocol", out var pf5)) protocolFile = pf5;
                    else if (parameters.TryGetValue("ProtocolName", out var pf6)) protocolFile = pf6;
                    else if (parameters.TryGetValue("protocol_name", out var pf7)) protocolFile = pf7;
                    
                    _logger.LogInformation("[PnP] RunMeasurement - ProtocolFile='{Protocol}', IsConnected={Connected}", 
                        protocolFile ?? "(null)", _tecanBridge.IsConnected);
                    
                    if (!string.IsNullOrEmpty(protocolFile))
                    {
                        // Get optional plate_id parameter
                        parameters.TryGetValue("plate_id", out var measurePlateId);
                        if (string.IsNullOrEmpty(measurePlateId))
                            parameters.TryGetValue("PlateId", out measurePlateId);
                        if (string.IsNullOrEmpty(measurePlateId))
                            parameters.TryGetValue("PlateID", out measurePlateId);
                        var actualPlateId = measurePlateId ?? $"PLATE_{DateTime.Now:yyyyMMdd_HHmmss}";
                        
                        _logger.LogInformation("[PnP] RunMeasurement - Calling TecanBridge.RunMeasurementAsync...");
                        
                        // Execute measurement
                        var measurementResult = await _tecanBridge.RunMeasurementAsync(protocolFile!, actualPlateId);
                        
                        _logger.LogInformation("[PnP] RunMeasurement - TecanBridge returned: Success={Success}, Error={Error}", 
                            measurementResult.Success, measurementResult.ErrorMessage ?? "(none)");
                        
                        result["success"] = measurementResult.Success.ToString();
                        result["error_message"] = measurementResult.ErrorMessage ?? "";
                        result["xml_file_path"] = measurementResult.XmlFilePath ?? "";
                        result["excel_file_path"] = measurementResult.ExcelFilePath ?? "";
                        result["plate_id"] = measurementResult.PlateId ?? "";
                        result["measurement_type"] = measurementResult.MeasurementType;
                        
                        // Convert to AnIML if XML exists and measurement succeeded
                        if (measurementResult.Success && File.Exists(measurementResult.XmlFilePath))
                        {
                            try
                            {
                                parameters.TryGetValue("sample_set_id", out var sampleSetId);
                                var converter = new TecanToAnIMLConverter(
                                    sampleSetId ?? "SampleSet_" + DateTime.Now.ToString("yyyyMMdd"), 
                                    actualPlateId);
                                var animlDoc = converter.ConvertFromFile(measurementResult.XmlFilePath!);
                                
                                // Save AnIML file
                                if (!string.IsNullOrEmpty(measurementResult.AnIMLFilePath))
                                {
                                    animlDoc.SaveToFile(measurementResult.AnIMLFilePath!);
                                    _logger.LogInformation("AnIML document created: {Path}", measurementResult.AnIMLFilePath);
                                }
                            }
                            catch (Exception ex)
                            {
                                _logger.LogWarning(ex, "Failed to convert to AnIML: {Message}", ex.Message);
                            }
                        }
                        result["animl_file_path"] = measurementResult.AnIMLFilePath ?? "";
                    }
                    else
                    {
                        var receivedParams = string.Join(", ", parameters.Keys);
                        throw new ArgumentException($"Missing protocol file parameter. Expected one of: protocol_file, ProtocolFile, ProtocolPath, protocol, Protocol, ProtocolName. Received parameters: [{receivedParams}]");
                    }
                    break;

                case "ListProtocols":
                    var listResp = await _plateReaderService.ListProtocols(new ListProtocolsRequest(), context);
                    result["protocols"] = string.Join(",", listResp.Protocols);
                    result["count"] = listResp.Protocols.Count.ToString();
                    break;

                case "GetAnIMLResult":
                    if (parameters.TryGetValue("plate_id", out var plateId))
                    {
                        var animlResp = await _plateReaderService.GetAnIMLResult(
                            new GetAnIMLResultRequest { PlateId = plateId }, context);
                        // AnimlDocument is bytes - convert to base64
                        result["success"] = (animlResp.AnimlDocument != null && animlResp.AnimlDocument.Length > 0).ToString();
                        result["animl_xml"] = animlResp.AnimlDocument != null 
                            ? Encoding.UTF8.GetString(animlResp.AnimlDocument.ToByteArray())
                            : "";
                    }
                    else
                    {
                        throw new ArgumentException("Missing 'plate_id' parameter");
                    }
                    break;

                case "GetStatus":
                    result["connected"] = _tecanBridge.IsConnected.ToString();
                    result["status"] = _tecanBridge.CurrentStatus;
                    result["temperature"] = _tecanBridge.GetCurrentTemperature().ToString("F1");
                    break;

                default:
                    throw new ArgumentException($"Unknown command: {command}");
            }

            return result;
        }

        /// <summary>
        /// Get property value generically.
        /// </summary>
        public override async Task<PropertyResponse> GetProperty(
            GetPropertyRequest request,
            ServerCallContext context)
        {
            _logger.LogInformation("[PnP] GetProperty: {Property}", request.Property);

            var response = new PropertyResponse { Success = true };

            switch (request.Property)
            {
                case "IsConnected":
                    response.Value = _tecanBridge.IsConnected.ToString();
                    response.DataType = "bool";
                    break;

                case "OperationalStatus":
                    response.Value = _tecanBridge.CurrentStatus;
                    response.DataType = "string";
                    break;

                case "CurrentTemperature":
                    response.Value = _tecanBridge.GetCurrentTemperature().ToString("F1");
                    response.DataType = "double";
                    break;

                case "InstrumentInfo":
                    var instrumentInfo = _tecanBridge.GetInstrumentInfo();
                    response.Value = JsonSerializer.Serialize(new
                    {
                        serial = instrumentInfo?.SerialNumber ?? "",
                        firmware = instrumentInfo?.FirmwareVersion ?? "",
                        product = instrumentInfo?.ProductName ?? "",
                        simulated = instrumentInfo?.IsSimulated ?? true
                    });
                    response.DataType = "object";
                    break;

                default:
                    response.Success = false;
                    response.Error = $"Unknown property: {request.Property}";
                    break;
            }

            return response;
        }

        #region Helper Methods

        private static Command CreateCommand(
            string id,
            string displayName,
            string description,
            Parameter[]? parameters = null,
            bool isObservable = false)
        {
            var cmd = new Command
            {
                Identifier = id,
                DisplayName = displayName,
                Description = description,
                Observable = isObservable
            };

            if (parameters != null)
            {
                cmd.Parameters.AddRange(parameters);
            }

            return cmd;
        }

        private static Parameter CreateParameter(
            string name,
            string type,
            string description,
            bool required,
            string? defaultValue = null)
        {
            return new Parameter
            {
                Identifier = name,
                DisplayName = name,
                Description = description,
                DataType = type,
                Required = required,
                DefaultValue = defaultValue ?? ""
            };
        }

        private static Property CreateProperty(
            string id,
            string displayName,
            string description,
            string type,
            bool isObservable)
        {
            return new Property
            {
                Identifier = id,
                DisplayName = displayName,
                Description = description,
                DataType = type,
                Observable = isObservable,
                Readonly = true
            };
        }

        #endregion
    }
}
