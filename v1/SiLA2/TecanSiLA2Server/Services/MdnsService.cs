// MdnsService.cs - mDNS/DNS-SD Service Registration for SiLA2 Discovery
// Permette la scoperta automatica del server Tecan sulla rete locale

using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Threading.Tasks;
using Makaretu.Dns;
using Microsoft.Extensions.Logging;

namespace TecanSiLA2Server.Services
{
    /// <summary>
    /// Servizio mDNS per registrare il server SiLA2 per la scoperta automatica.
    /// Usa il formato standard SiLA2: _sila2._tcp.local
    /// </summary>
    public class MdnsService : IDisposable
    {
        private readonly ILogger<MdnsService> _logger;
        private readonly string _serverName;
        private readonly int _port;
        private readonly string[] _features;
        
        private ServiceDiscovery? _serviceDiscovery;
        private ServiceProfile? _serviceProfile;
        private bool _isRegistered;
        private bool _disposed;

        /// <summary>
        /// Crea un nuovo servizio mDNS per la registrazione SiLA2
        /// </summary>
        /// <param name="logger">Logger per diagnostica</param>
        /// <param name="serverName">Nome del server (es. "TecanM200")</param>
        /// <param name="port">Porta gRPC del server</param>
        /// <param name="features">Features SiLA2 esposte (es. "PlateReader", "SiLA2Common")</param>
        public MdnsService(ILogger<MdnsService> logger, string serverName, int port, params string[] features)
        {
            _logger = logger;
            _serverName = serverName;
            _port = port;
            _features = features;
        }

        /// <summary>
        /// Registra il server per la scoperta mDNS
        /// </summary>
        public async Task RegisterAsync()
        {
            if (_isRegistered)
            {
                _logger.LogWarning("[mDNS] Già registrato, skip");
                return;
            }

            try
            {
                _logger.LogInformation("[mDNS] Inizializzazione registrazione per {Server}...", _serverName);

                // Ottieni le informazioni di rete
                var ipAddresses = GetLocalIPAddresses();
                if (!ipAddresses.Any())
                {
                    _logger.LogWarning("[mDNS] Nessun indirizzo IP locale trovato");
                    return;
                }

                var primaryIP = ipAddresses.First();
                _logger.LogInformation("[mDNS] IP locale: {IP}", primaryIP);

                // Crea il profilo del servizio SiLA2
                // Formato standard: _sila2._tcp.local
                var serviceName = $"{_serverName}._sila2._tcp.local";
                var hostName = $"{_serverName}.local";

                _serviceProfile = new ServiceProfile(
                    instanceName: _serverName,
                    serviceName: "_sila2._tcp",
                    port: (ushort)_port
                );

                // Aggiungi i TXT record con le informazioni del server
                var txtRecords = new Dictionary<string, string>
                {
                    { "server_type", "SiLA2" },
                    { "server_name", _serverName },
                    { "server_uuid", Guid.NewGuid().ToString() },
                    { "features", string.Join(",", _features) },
                    { "vendor", "Tecan" },
                    { "model", "Infinite M200 Pro" },
                    { "category", "instruments.platereader" }
                };

                foreach (var kv in txtRecords)
                {
                    _serviceProfile.AddProperty(kv.Key, kv.Value);
                }

                // Crea il service discovery e annuncia
                _serviceDiscovery = new ServiceDiscovery();
                _serviceDiscovery.Advertise(_serviceProfile);

                _isRegistered = true;
                _logger.LogInformation("[mDNS] ✓ Registrato: {Service} su porta {Port}", serviceName, _port);
                _logger.LogInformation("[mDNS]   Features: {Features}", string.Join(", ", _features));
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "[mDNS] Errore durante la registrazione: {Message}", ex.Message);
                // Non propaghiamo l'errore - mDNS è opzionale
            }

            await Task.CompletedTask;
        }

        /// <summary>
        /// Rimuove la registrazione mDNS
        /// </summary>
        public async Task UnregisterAsync()
        {
            if (!_isRegistered)
            {
                return;
            }

            try
            {
                _logger.LogInformation("[mDNS] Rimozione registrazione...");

                if (_serviceProfile != null && _serviceDiscovery != null)
                {
                    _serviceDiscovery.Unadvertise(_serviceProfile);
                }

                _isRegistered = false;
                _logger.LogInformation("[mDNS] ✓ Registrazione rimossa");
            }
            catch (Exception ex)
            {
                _logger.LogWarning("[mDNS] Errore durante la rimozione: {Message}", ex.Message);
            }

            await Task.CompletedTask;
        }

        /// <summary>
        /// Ottiene gli indirizzi IP locali della macchina
        /// </summary>
        private List<IPAddress> GetLocalIPAddresses()
        {
            var addresses = new List<IPAddress>();

            try
            {
                foreach (var iface in NetworkInterface.GetAllNetworkInterfaces())
                {
                    // Salta interfacce non attive o virtuali
                    if (iface.OperationalStatus != OperationalStatus.Up)
                        continue;

                    if (iface.NetworkInterfaceType == NetworkInterfaceType.Loopback)
                        continue;

                    var props = iface.GetIPProperties();
                    foreach (var addr in props.UnicastAddresses)
                    {
                        // Prendi solo IPv4
                        if (addr.Address.AddressFamily == AddressFamily.InterNetwork)
                        {
                            // Escludi link-local (169.254.x.x)
                            var bytes = addr.Address.GetAddressBytes();
                            if (bytes[0] == 169 && bytes[1] == 254)
                                continue;

                            addresses.Add(addr.Address);
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                _logger.LogWarning("[mDNS] Errore ottenendo indirizzi IP: {Message}", ex.Message);
            }

            return addresses;
        }

        public void Dispose()
        {
            if (_disposed)
                return;

            _disposed = true;

            try
            {
                if (_isRegistered)
                {
                    UnregisterAsync().GetAwaiter().GetResult();
                }

                _serviceDiscovery?.Dispose();
            }
            catch
            {
                // Ignora errori durante dispose
            }
        }
    }
}
