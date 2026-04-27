// TestClient.cs
// Client di test per il server SiLA2 Tecan
// Permette di inviare comandi interattivamente

using System;
using System.Text;
using System.Threading.Tasks;
using Grpc.Core;
using TecanSiLA2Server.Features;

namespace TestClient
{
    class Program
    {
        private static PlateReaderService.PlateReaderServiceClient? _client;
        private static Channel? _channel;

        static async Task Main(string[] args)
        {
            string host = "localhost";
            int port = 50051;

            if (args.Length >= 1) host = args[0];
            if (args.Length >= 2) int.TryParse(args[1], out port);

            Console.WriteLine("===========================================");
            Console.WriteLine("  Tecan SiLA2 Server - Test Client");
            Console.WriteLine("===========================================");
            Console.WriteLine($"Connecting to {host}:{port}...\n");

            try
            {
                _channel = new Channel($"{host}:{port}", ChannelCredentials.Insecure);
                _client = new PlateReaderService.PlateReaderServiceClient(_channel);

                Console.WriteLine("Connected! Type 'help' for available commands.\n");

                await RunInteractiveMode();
            }
            catch (Exception ex)
            {
                Console.WriteLine($"Connection error: {ex.Message}");
            }
            finally
            {
                if (_channel != null)
                    await _channel.ShutdownAsync();
            }
        }

        static async Task RunInteractiveMode()
        {
            bool running = true;

            while (running)
            {
                Console.Write("\nTecan> ");
                string? input = Console.ReadLine()?.Trim().ToLower();

                if (string.IsNullOrEmpty(input)) continue;

                try
                {
                    switch (input)
                    {
                        case "help":
                            ShowHelp();
                            break;

                        case "status":
                            await GetStatus();
                            break;

                        case "connect":
                            await Connect();
                            break;

                        case "disconnect":
                            await Disconnect();
                            break;

                        case "platein":
                            await PlateIn();
                            break;

                        case "plateout":
                            await PlateOut();
                            break;

                        case "temp":
                            Console.Write("Temperature (°C): ");
                            if (double.TryParse(Console.ReadLine(), out double temp))
                                await SetTemperature(temp);
                            else
                                Console.WriteLine("Invalid temperature");
                            break;

                        case "tempoff":
                            await TurnOffTemperature();
                            break;

                        case "protocols":
                            await ListProtocols();
                            break;

                        case "run":
                            Console.Write("Protocol file name: ");
                            string? protocol = Console.ReadLine();
                            Console.Write("Plate ID: ");
                            string? plateId = Console.ReadLine();
                            Console.Write("Sample Set ID: ");
                            string? sampleSetId = Console.ReadLine();
                            if (!string.IsNullOrEmpty(protocol))
                                await RunMeasurement(protocol, plateId ?? "TEST_PLATE", sampleSetId ?? "TEST_SET");
                            break;

                        case "result":
                            Console.Write("Plate ID: ");
                            string? resultPlateId = Console.ReadLine();
                            if (!string.IsNullOrEmpty(resultPlateId))
                                await GetAnIMLResult(resultPlateId);
                            break;

                        case "exit":
                        case "quit":
                            running = false;
                            Console.WriteLine("Goodbye!");
                            break;

                        default:
                            Console.WriteLine($"Unknown command: {input}. Type 'help' for available commands.");
                            break;
                    }
                }
                catch (RpcException ex)
                {
                    Console.WriteLine($"[ERROR] gRPC error: {ex.Status.Detail}");
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"[ERROR] {ex.Message}");
                }
            }
        }

        static void ShowHelp()
        {
            Console.WriteLine(@"
Available Commands:
-------------------
  status      - Get current instrument status
  connect     - Connect to Tecan instrument
  disconnect  - Disconnect from instrument
  platein     - Move carrier in
  plateout    - Move carrier out
  temp        - Set temperature
  tempoff     - Turn off temperature control
  protocols   - List available protocols
  run         - Run a measurement protocol
  result      - Get AnIML result by plate ID
  exit/quit   - Exit the client
");
        }

        static async Task GetStatus()
        {
            Console.WriteLine("Getting status...");

            var connected = await _client!.GetIsConnectedAsync(new GetIsConnectedRequest());
            var status = await _client.GetOperationalStatusAsync(new GetOperationalStatusRequest());
            var temp = await _client.GetCurrentTemperatureAsync(new GetCurrentTemperatureRequest());
            var info = await _client.GetInstrumentInfoAsync(new GetInstrumentInfoRequest());

            Console.WriteLine($@"
Instrument Status:
  Connected:    {connected.Value}
  Status:       {status.Value}
  Temperature:  {temp.Value:F1} °C
  Product:      {info.Value?.ProductName ?? "N/A"}
  Serial:       {info.Value?.SerialNumber ?? "N/A"}
  Firmware:     {info.Value?.FirmwareVersion ?? "N/A"}
  Simulated:    {info.Value?.IsSimulated ?? false}
");
        }

        static async Task Connect()
        {
            Console.Write("Connection string (press Enter for USB only, 'sim' for simulator): ");
            string? connStr = Console.ReadLine();
            if (string.IsNullOrWhiteSpace(connStr))
                connStr = "usb";  // DEFAULT: Solo USB, come il bridge originale

            Console.WriteLine($"Connecting with: {connStr}");
            Console.WriteLine("Waiting for instrument selection... (this may take a while)");
            
            // Timeout di 2 minuti per permettere la selezione dello strumento
            var deadline = DateTime.UtcNow.AddMinutes(2);
            var options = new Grpc.Core.CallOptions(deadline: deadline);
            
            try
            {
                var response = await _client!.ConnectAsync(new ConnectRequest { ConnectionString = connStr }, options);
                Console.WriteLine($"Result: {(response.Success ? "SUCCESS" : "FAILED")}");
            }
            catch (Grpc.Core.RpcException ex) when (ex.StatusCode == Grpc.Core.StatusCode.DeadlineExceeded)
            {
                Console.WriteLine("Connection timed out. The instrument selection may still be in progress.");
            }
        }

        static async Task Disconnect()
        {
            Console.WriteLine("Disconnecting from instrument...");
            var response = await _client!.DisconnectAsync(new DisconnectRequest());
            Console.WriteLine($"Result: {(response.Success ? "SUCCESS" : "FAILED")}");
        }

        static async Task PlateIn()
        {
            Console.WriteLine("Moving carrier in...");
            var response = await _client!.PlateInAsync(new PlateInRequest());
            Console.WriteLine($"Result: {(response.Success ? "SUCCESS" : "FAILED")}");
        }

        static async Task PlateOut()
        {
            Console.WriteLine("Moving carrier out...");
            var response = await _client!.PlateOutAsync(new PlateOutRequest());
            Console.WriteLine($"Result: {(response.Success ? "SUCCESS" : "FAILED")}");
        }

        static async Task SetTemperature(double temperature)
        {
            Console.WriteLine($"Setting temperature to {temperature}°C...");
            var response = await _client!.SetTemperatureAsync(new SetTemperatureRequest
            {
                TargetTemperature = temperature
            });
            Console.WriteLine($"Result: {(response.Success ? "SUCCESS" : "FAILED")}");
        }

        static async Task TurnOffTemperature()
        {
            Console.WriteLine("Turning off temperature control...");
            var response = await _client!.TurnOffTemperatureAsync(new TurnOffTemperatureRequest());
            Console.WriteLine($"Result: {(response.Success ? "SUCCESS" : "FAILED")}");
        }

        static async Task ListProtocols()
        {
            Console.WriteLine("Listing protocols...");
            var response = await _client!.ListProtocolsAsync(new ListProtocolsRequest());

            Console.WriteLine($"\nFound {response.Protocols.Count} protocols:");
            foreach (var protocol in response.Protocols)
            {
                Console.WriteLine($"  - {protocol}");
            }
        }

        static async Task RunMeasurement(string protocolFile, string plateId, string sampleSetId)
        {
            Console.WriteLine($"Running measurement...");
            Console.WriteLine($"  Protocol: {protocolFile}");
            Console.WriteLine($"  Plate ID: {plateId}");
            Console.WriteLine($"  Sample Set: {sampleSetId}");

            var request = new RunMeasurementRequest
            {
                ProtocolFile = protocolFile,
                PlateId = plateId,
                SampleSetId = sampleSetId
            };

            using var call = _client!.RunMeasurement(request);

            while (await call.ResponseStream.MoveNext())
            {
                var response = call.ResponseStream.Current;
                if (response.IsIntermediate)
                {
                    Console.WriteLine($"  Progress: {response.Progress}% - {response.StatusMessage}");
                }
                else
                {
                    Console.WriteLine($"\nMeasurement Complete!");
                    var result = response.MeasurementResult;
                    if (result != null)
                    {
                        Console.WriteLine($"  AnIML Path: {result.AnimlFilePath}");
                        Console.WriteLine($"  Excel Path: {result.ExcelFilePath}");
                        Console.WriteLine($"  Type: {result.MeasurementType}");
                    }
                }
            }
        }

        static async Task GetAnIMLResult(string plateId)
        {
            Console.WriteLine($"Getting AnIML result for plate: {plateId}...");

            var response = await _client!.GetAnIMLResultAsync(new GetAnIMLResultRequest
            {
                PlateId = plateId
            });

            if (response.AnimlDocument != null && response.AnimlDocument.Length > 0)
            {
                string doc = Encoding.UTF8.GetString(response.AnimlDocument.ToByteArray());
                Console.WriteLine($"\nAnIML Document (first 2000 chars):");
                Console.WriteLine(new string('-', 50));
                Console.WriteLine(doc.Length > 2000 ? doc.Substring(0, 2000) + "\n..." : doc);
                Console.WriteLine(new string('-', 50));
            }
            else
            {
                Console.WriteLine("No AnIML document found for this plate ID.");
            }
        }
    }
}
