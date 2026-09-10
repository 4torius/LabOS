// PlateTypeLibrary.cs
// Piccola libreria piastra->{file,range} per sovrascrivere il tipo di
// piastra hardcoded in un .mdfx (vedi TecanBridge.RewritePlateType).
// Specchia in scala ridotta la filosofia HAL usata per Opentrons
// (Library/Labware/Plates/*.json): un file JSON esterno che il laboratorio
// puo' estendere senza ricompilare nulla.

using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using Microsoft.Extensions.Logging;

namespace TecanSiLA2Server.Instrument
{
    /// <summary>
    /// Una singola voce della libreria: il codice piastra della libreria
    /// Tecan interna e il range pozzetti completo corrispondente. I due
    /// campi sono sempre accoppiati -- vanno cambiati insieme.
    /// </summary>
    public sealed class PlateTypeDefinition
    {
        public string File { get; }
        public string Range { get; }

        public PlateTypeDefinition(string file, string range)
        {
            File = file;
            Range = range;
        }
    }

    /// <summary>
    /// Carica Library/Analysis/PlateTypes.json (path configurabile via
    /// ServerConfiguration.PlateTypesPath). Chiavi che iniziano con "_" sono
    /// commenti/metadati e vengono ignorate. Una voce priva sia di "file" sia
    /// di "range" viene scartata con un warning, mai usata a meta'.
    /// </summary>
    public static class PlateTypeLibrary
    {
        public static Dictionary<string, PlateTypeDefinition> Load(string path, ILogger logger)
        {
            var result = new Dictionary<string, PlateTypeDefinition>(StringComparer.OrdinalIgnoreCase);

            if (string.IsNullOrEmpty(path) || !File.Exists(path))
            {
                logger.LogInformation(
                    "[PlateTypeLibrary] Nessun PlateTypes.json trovato in '{Path}' -- il parametro PlateType di RunMeasurement rimane un no-op (usa sempre il default del .mdfx).",
                    path);
                return result;
            }

            try
            {
                string json = File.ReadAllText(path);
                using var doc = JsonDocument.Parse(json);

                foreach (var prop in doc.RootElement.EnumerateObject())
                {
                    if (prop.Name.StartsWith("_", StringComparison.Ordinal))
                        continue; // commento/metadato, non una voce piastra

                    if (prop.Value.ValueKind != JsonValueKind.Object
                        || !prop.Value.TryGetProperty("file", out var fileEl)
                        || !prop.Value.TryGetProperty("range", out var rangeEl)
                        || fileEl.ValueKind != JsonValueKind.String
                        || rangeEl.ValueKind != JsonValueKind.String)
                    {
                        logger.LogWarning(
                            "[PlateTypeLibrary] Voce '{Key}' in {Path} incompleta o malformata (servono sia 'file' che 'range' come stringhe) -- ignorata.",
                            prop.Name, path);
                        continue;
                    }

                    result[prop.Name] = new PlateTypeDefinition(fileEl.GetString()!, rangeEl.GetString()!);
                }

                logger.LogInformation("[PlateTypeLibrary] Caricate {Count} definizioni piastra da {Path}", result.Count, path);
            }
            catch (Exception ex)
            {
                logger.LogWarning(ex, "[PlateTypeLibrary] Impossibile leggere {Path} -- il parametro PlateType rimane un no-op.", path);
            }

            return result;
        }
    }
}
