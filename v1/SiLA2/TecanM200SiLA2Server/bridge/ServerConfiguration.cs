// ServerConfiguration.cs
// Classe per la gestione della configurazione del server SiLA2

using System;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace TecanSiLA2Server
{
    /// <summary>
    /// Configurazione del server SiLA2 per Tecan M200 Pro
    /// </summary>
    public class ServerConfiguration
    {
        // Server settings
        public string ServerName { get; set; } = "Tecan M200 Pro";
        public int GrpcPort { get; set; } = 50051;
        public string BindAddress { get; set; } = "0.0.0.0";
        
        // Paths
        public string ProtocolsPath { get; set; } = "";
        public string ResultsPath { get; set; } = "";
        
        // Connection
        public string DefaultConnectionString { get; set; } = "";
        public bool AutoConnectOnStartup { get; set; } = false;
        
        // Output
        public bool GenerateCsv { get; set; } = false;  // Disabilitato di default
        public bool GenerateExcel { get; set; } = true;  // Per operatori umani
        public bool GenerateAnIML { get; set; } = true;  // Standard ASTM
        
        // Advanced
        public int ConnectionTimeoutSeconds { get; set; } = 60;
        public int MeasurementTimeoutSeconds { get; set; } = 0;
        public bool ShowPolarization { get; set; } = true;
        public bool ShowAnisotropy { get; set; } = true;

        // mDNS — disable when running as internal bridge (Python SiLA2 server handles discovery)
        public bool EnableMdns { get; set; } = false;
        
        /// <summary>
        /// Risolve i percorsi relativi in percorsi assoluti
        /// </summary>
        public void ResolvePaths(string basePath)
        {
            if (string.IsNullOrEmpty(ProtocolsPath))
                ProtocolsPath = Path.Combine(basePath, "Protocols");
            else if (!Path.IsPathRooted(ProtocolsPath))
                ProtocolsPath = Path.Combine(basePath, ProtocolsPath);
                
            if (string.IsNullOrEmpty(ResultsPath))
                ResultsPath = Path.Combine(basePath, "Results");
            else if (!Path.IsPathRooted(ResultsPath))
                ResultsPath = Path.Combine(basePath, ResultsPath);
        }
        
        /// <summary>
        /// Carica la configurazione da file JSON
        /// </summary>
        public static ServerConfiguration Load(string configPath)
        {
            if (!File.Exists(configPath))
                return new ServerConfiguration();
                
            try
            {
                string json = File.ReadAllText(configPath);
                
                // Rimuovi i commenti dal JSON (non standard ma utile per config)
                json = RemoveJsonComments(json);
                
                var options = new JsonSerializerOptions
                {
                    PropertyNameCaseInsensitive = true,
                    ReadCommentHandling = JsonCommentHandling.Skip,
                    AllowTrailingCommas = true
                };
                
                var root = JsonSerializer.Deserialize<JsonElement>(json, options);
                
                if (root.TryGetProperty("TecanSiLA2Server", out JsonElement serverSection))
                {
                    return JsonSerializer.Deserialize<ServerConfiguration>(
                        serverSection.GetRawText(), options) ?? new ServerConfiguration();
                }
                
                return JsonSerializer.Deserialize<ServerConfiguration>(json, options) 
                    ?? new ServerConfiguration();
            }
            catch (Exception ex)
            {
                Console.WriteLine($"Warning: Failed to load config file: {ex.Message}");
                return new ServerConfiguration();
            }
        }
        
        /// <summary>
        /// Salva la configurazione su file JSON
        /// </summary>
        public void Save(string configPath)
        {
            var options = new JsonSerializerOptions
            {
                WriteIndented = true
            };
            
            var wrapper = new { TecanSiLA2Server = this };
            string json = JsonSerializer.Serialize(wrapper, options);
            File.WriteAllText(configPath, json);
        }
        
        private static string RemoveJsonComments(string json)
        {
            var result = new System.Text.StringBuilder();
            bool inString = false;
            bool inLineComment = false;
            bool inBlockComment = false;
            
            for (int i = 0; i < json.Length; i++)
            {
                char c = json[i];
                char next = i + 1 < json.Length ? json[i + 1] : '\0';
                
                if (inLineComment)
                {
                    if (c == '\n')
                    {
                        inLineComment = false;
                        result.Append(c);
                    }
                    continue;
                }
                
                if (inBlockComment)
                {
                    if (c == '*' && next == '/')
                    {
                        inBlockComment = false;
                        i++;
                    }
                    continue;
                }
                
                if (inString)
                {
                    result.Append(c);
                    if (c == '"' && (i == 0 || json[i - 1] != '\\'))
                        inString = false;
                    continue;
                }
                
                if (c == '"')
                {
                    inString = true;
                    result.Append(c);
                    continue;
                }
                
                if (c == '/' && next == '/')
                {
                    inLineComment = true;
                    continue;
                }
                
                if (c == '/' && next == '*')
                {
                    inBlockComment = true;
                    i++;
                    continue;
                }
                
                result.Append(c);
            }
            
            return result.ToString();
        }
    }
}
