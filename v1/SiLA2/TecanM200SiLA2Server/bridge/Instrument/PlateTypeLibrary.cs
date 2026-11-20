// PlateTypeLibrary.cs
// Le piastre che RunMeasurement puo' usare al posto di quella scritta nel
// .mdfx (vedi TecanBridge.RewritePlateType).
//
// Le piastre sono quelle di Library/Labware/Plates/*.plate.json, le stesse
// che usa l'Opentrons: una piastra e' misurabile col Tecan quando il suo file
// ha il campo "tecan_plate_name", il nome della definizione Tecan (.pdfx) che
// descrive la stessa piastra. Il nome che si passa a RunMeasurement e' l'"id"
// della piastra. Guida: docs/NUOVA_PIASTRA.md.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Xml.Linq;
using Microsoft.Extensions.Logging;

namespace TecanSiLA2Server.Instrument
{
    /// <summary>
    /// Una piastra misurabile: il nome della definizione Tecan (.pdfx) e il
    /// range pozzetti completo corrispondente. I due vanno sempre insieme.
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
    /// Le piastre accettate, e quelle scartate con il motivo, cosi' che una
    /// misura che ne chiede una scartata dica perche' invece di "non trovata".
    /// </summary>
    public sealed class PlateTypeCatalog
    {
        public Dictionary<string, PlateTypeDefinition> Plates { get; } =
            new Dictionary<string, PlateTypeDefinition>(StringComparer.OrdinalIgnoreCase);

        public Dictionary<string, string> Rejected { get; } =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
    }

    public static class PlateTypeLibrary
    {
        /// <summary>
        /// Legge platesDir/*.plate.json. Per ogni piastra con "tecan_plate_name"
        /// controlla, se pdfxDir esiste, che il .pdfx ci sia e che abbia le
        /// stesse righe e colonne della piastra: una definizione sbagliata non
        /// da' errore sullo strumento, misura semplicemente nel punto sbagliato.
        /// </summary>
        public static PlateTypeCatalog Load(string platesDir, string pdfxDir, ILogger logger)
        {
            var catalog = new PlateTypeCatalog();

            if (string.IsNullOrEmpty(platesDir) || !Directory.Exists(platesDir))
            {
                logger.LogWarning(
                    "[PlateTypeLibrary] Cartella piastre '{Path}' non trovata -- il parametro PlateType di RunMeasurement non accettera' nessuna piastra.",
                    platesDir);
                return catalog;
            }

            bool canCheckPdfx = !string.IsNullOrEmpty(pdfxDir) && Directory.Exists(pdfxDir);
            if (!canCheckPdfx)
            {
                logger.LogWarning(
                    "[PlateTypeLibrary] Cartella delle definizioni Tecan '{Path}' non trovata -- le piastre vengono accettate senza controllare il .pdfx.",
                    pdfxDir);
            }

            foreach (var path in Directory.GetFiles(platesDir, "*.plate.json").OrderBy(p => p, StringComparer.OrdinalIgnoreCase))
            {
                string fileName = Path.GetFileName(path);
                try
                {
                    using var doc = JsonDocument.Parse(System.IO.File.ReadAllText(path));
                    var root = doc.RootElement;

                    string tecanName = GetString(root, "tecan_plate_name");
                    if (string.IsNullOrWhiteSpace(tecanName))
                        continue; // piastra solo per l'Opentrons

                    string id = GetString(root, "id");
                    if (string.IsNullOrWhiteSpace(id))
                    {
                        logger.LogWarning("[PlateTypeLibrary] {File}: ha tecan_plate_name ma nessun 'id' -- ignorata.", fileName);
                        continue;
                    }

                    if (catalog.Plates.ContainsKey(id))
                    {
                        // Only a warning: the first file with this id stays
                        // usable, so it must not be listed as rejected.
                        logger.LogWarning(
                            "[PlateTypeLibrary] {File} usa l'id '{Id}' gia' usato da un'altra piastra -- ignorato, tenuta la prima.",
                            fileName, id);
                        continue;
                    }

                    int rows = GetInt(root, "rows");
                    int columns = GetInt(root, "columns");
                    if (rows <= 0 || columns <= 0)
                    {
                        Reject(catalog, logger, id, $"{fileName}: servono 'rows' e 'columns'");
                        continue;
                    }

                    string range = GetString(root, "tecan_plate_range");
                    if (string.IsNullOrWhiteSpace(range))
                    {
                        if (rows > 26)
                        {
                            Reject(catalog, logger, id, $"{fileName}: oltre 26 righe serve 'tecan_plate_range' esplicito");
                            continue;
                        }
                        range = $"A1:{(char)('A' + rows - 1)}{columns}";
                    }

                    if (canCheckPdfx)
                    {
                        string problem = CheckPdfx(pdfxDir, tecanName, rows, columns);
                        if (problem != null)
                        {
                            Reject(catalog, logger, id, $"{fileName}: {problem}");
                            continue;
                        }
                    }

                    catalog.Plates[id] = new PlateTypeDefinition(tecanName.Trim(), range.Trim());
                }
                catch (Exception ex)
                {
                    logger.LogWarning(ex, "[PlateTypeLibrary] Impossibile leggere {File} -- ignorata.", fileName);
                }
            }

            logger.LogInformation("[PlateTypeLibrary] {Count} piastre misurabili da {Path}: {Ids}",
                catalog.Plates.Count, platesDir, string.Join(", ", catalog.Plates.Keys));
            return catalog;
        }

        /// <summary>null se il .pdfx esiste e ha la stessa griglia, altrimenti il motivo.</summary>
        private static string CheckPdfx(string pdfxDir, string tecanName, int rows, int columns)
        {
            string pdfxPath = Path.Combine(pdfxDir, tecanName.Trim() + ".pdfx");
            if (!System.IO.File.Exists(pdfxPath))
                return $"definizione Tecan '{tecanName}.pdfx' non trovata in {pdfxDir}";

            XElement dims;
            try
            {
                dims = XDocument.Load(pdfxPath).Descendants()
                    .FirstOrDefault(e => e.Name.LocalName == "DefinitionRectanglePlateBaseDimensions");
            }
            catch (Exception ex)
            {
                return $"'{tecanName}.pdfx' illeggibile ({ex.Message})";
            }
            if (dims == null)
                return null; // formato diverso: niente da confrontare, accettata

            int.TryParse((string)dims.Attribute("nrOfRows"), out int pdfxRows);
            int.TryParse((string)dims.Attribute("nrOfColumns"), out int pdfxColumns);
            if (pdfxRows > 0 && pdfxColumns > 0 && (pdfxRows != rows || pdfxColumns != columns))
                return $"'{tecanName}.pdfx' ha {pdfxRows}x{pdfxColumns} pozzetti, la piastra {rows}x{columns}";
            return null;
        }

        private static void Reject(PlateTypeCatalog catalog, ILogger logger, string id, string reason)
        {
            if (!catalog.Rejected.ContainsKey(id))
                catalog.Rejected[id] = reason;
            logger.LogWarning("[PlateTypeLibrary] Piastra '{Id}' scartata: {Reason}", id, reason);
        }

        private static string GetString(JsonElement root, string name) =>
            root.TryGetProperty(name, out var el) && el.ValueKind == JsonValueKind.String ? el.GetString() : null;

        private static int GetInt(JsonElement root, string name) =>
            root.TryGetProperty(name, out var el) && el.ValueKind == JsonValueKind.Number && el.TryGetInt32(out int v) ? v : 0;
    }
}
