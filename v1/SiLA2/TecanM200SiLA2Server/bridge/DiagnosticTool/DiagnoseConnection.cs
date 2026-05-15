// DiagnoseConnection.cs
// Tool di diagnostica per verificare la connessione al Tecan M200 Pro
// Uso: eseguire questo tool per identificare problemi di connessione

using System;
using System.Collections.Generic;
using System.Threading;
using System.Windows.Forms;
using Tecan.At.Common.DocumentManagement;
using Tecan.At.Common.DocumentManagement.Reader;
using Tecan.At.Instrument.Common;
using Tecan.At.Measurement.Common;
using Tecan.At.Measurement.Server;

namespace TecanDiagnostic
{
    class Program
    {
        [STAThread]
        static void Main(string[] args)
        {
            Console.WriteLine("╔═══════════════════════════════════════════════════════════════╗");
            Console.WriteLine("║     TECAN CONNECTION DIAGNOSTIC TOOL                          ║");
            Console.WriteLine("║     Per Infinite M200 Pro                                     ║");
            Console.WriteLine("╚═══════════════════════════════════════════════════════════════╝");
            Console.WriteLine();

            try
            {
                // 1. Inizializzazione SDK
                Console.WriteLine("[1] Inizializzazione SDK Tecan...");
                ObjectFactory.AddEntryPoint(new DocumentEntryPoint());
                Console.ForegroundColor = ConsoleColor.Green;
                Console.WriteLine("    ✓ SDK inizializzato correttamente");
                Console.ResetColor();
            }
            catch (Exception ex)
            {
                Console.ForegroundColor = ConsoleColor.Red;
                Console.WriteLine($"    ✗ ERRORE SDK: {ex.Message}");
                Console.WriteLine("    → Verificare che le DLL Tecan siano nella cartella bin");
                Console.ResetColor();
                WaitAndExit();
                return;
            }

            // 2. Test connessioni
            Console.WriteLine();
            Console.WriteLine("[2] Test connessioni disponibili...");
            Console.WriteLine();

            var server = new MeasurementServer();

            // Test 1: Solo USB (strumenti reali)
            TestConnection(server, "USB Only (Real Instruments)", 
                "porttype=USB, type=READER");

            // Test 2: USB con dialogo
            TestConnection(server, "USB con Dialog Selezione", 
                "porttype=USB, type=READER, option=dialog");

            // Test 3: Simulatori
            TestConnection(server, "Simulatori", 
                "porttype=SIM, type=READER");

            // Test 4: Ultimo connesso
            TestConnection(server, "Ultimo Connesso (option=default)", 
                "porttype=USB|SIM, type=READER, option=default");

            // Test 5: USB specifico M200
            TestConnection(server, "USB M200 Specifico", 
                "porttype=USB, type=READER, option=Infinite");

            Console.WriteLine();
            Console.WriteLine("═══════════════════════════════════════════════════════════════");
            Console.WriteLine("SUGGERIMENTI:");
            Console.WriteLine("═══════════════════════════════════════════════════════════════");
            Console.WriteLine();
            Console.WriteLine("Se nessun strumento USB viene rilevato:");
            Console.WriteLine("  1. Verificare che il Tecan M200 sia acceso");
            Console.WriteLine("  2. Verificare il cavo USB");
            Console.WriteLine("  3. Aprire Gestione Dispositivi e cercare 'Tecan'");
            Console.WriteLine("  4. Se non presente, reinstallare i driver Tecan");
            Console.WriteLine("     (Cartella: C:\\Program Files\\Tecan\\iControl\\Drivers)");
            Console.WriteLine();
            Console.WriteLine("Se solo simulatori funzionano:");
            Console.WriteLine("  → Il driver USB non è installato correttamente");
            Console.WriteLine("  → Provare a reinstallare iControl completamente");
            Console.WriteLine();

            // Test interattivo
            Console.WriteLine("═══════════════════════════════════════════════════════════════");
            Console.Write("Vuoi provare una connessione interattiva? (s/n): ");
            if (Console.ReadLine()?.ToLower() == "s")
            {
                TryInteractiveConnection(server);
            }

            WaitAndExit();
        }

        static void TestConnection(MeasurementServer server, string testName, string connectionString)
        {
            Console.Write($"  Testing '{testName}'... ");
            try
            {
                // Timeout per evitare blocchi
                bool connected = false;
                var thread = new Thread(() =>
                {
                    try
                    {
                        connected = server.Connect(InstrumentConnectionMethod.LastConnection, connectionString);
                    }
                    catch { }
                });
                thread.SetApartmentState(ApartmentState.STA);
                thread.Start();
                
                if (!thread.Join(5000)) // 5 secondi timeout
                {
                    Console.ForegroundColor = ConsoleColor.Yellow;
                    Console.WriteLine("TIMEOUT (potrebbe richiedere interazione UI)");
                    Console.ResetColor();
                    return;
                }

                if (connected)
                {
                    var info = server.ConnectedReader.Information;
                    bool isSimulated = info.IsSimulated();
                    string productName = info.GetProductName();
                    string serial = info.GetInstrumentSerial();

                    Console.ForegroundColor = ConsoleColor.Green;
                    Console.WriteLine($"OK");
                    Console.ResetColor();
                    Console.WriteLine($"        Prodotto: {productName}");
                    Console.WriteLine($"        Seriale:  {serial}");
                    Console.WriteLine($"        Simulato: {(isSimulated ? "SÌ" : "NO")}");

                    server.Disconnect();
                }
                else
                {
                    Console.ForegroundColor = ConsoleColor.Red;
                    Console.WriteLine("FALLITO");
                    Console.ResetColor();
                }
            }
            catch (Exception ex)
            {
                Console.ForegroundColor = ConsoleColor.Red;
                Console.WriteLine($"ERRORE: {ex.Message}");
                Console.ResetColor();
            }
            Console.WriteLine();
        }

        static void TryInteractiveConnection(MeasurementServer server)
        {
            Console.WriteLine();
            Console.WriteLine("Tentativo connessione interattiva...");
            Console.WriteLine("(Apparirà una finestra di selezione strumento)");
            Console.WriteLine();

            var thread = new Thread(() =>
            {
                try
                {
                    // Forza il dialogo di selezione
                    if (server.Connect(InstrumentConnectionMethod.LastConnection, 
                        "porttype=USB|SIM, type=READER"))
                    {
                        var info = server.ConnectedReader.Information;
                        Console.ForegroundColor = ConsoleColor.Green;
                        Console.WriteLine($"CONNESSO A: {info.GetProductName()}");
                        Console.WriteLine($"Seriale: {info.GetInstrumentSerial()}");
                        Console.WriteLine($"Simulato: {info.IsSimulated()}");
                        Console.ResetColor();
                        server.Disconnect();
                    }
                    else
                    {
                        Console.ForegroundColor = ConsoleColor.Red;
                        Console.WriteLine("Connessione fallita o annullata");
                        Console.ResetColor();
                    }
                }
                catch (Exception ex)
                {
                    Console.ForegroundColor = ConsoleColor.Red;
                    Console.WriteLine($"Errore: {ex.Message}");
                    Console.ResetColor();
                }
            });
            thread.SetApartmentState(ApartmentState.STA);
            thread.Start();
            thread.Join();
        }

        static void WaitAndExit()
        {
            Console.WriteLine();
            Console.WriteLine("Premi INVIO per uscire...");
            Console.ReadLine();
        }
    }
}
