// TecanToAnIMLConverter.cs
// Converte i risultati XML nativi del Tecan in formato AnIML
// AnIML (Analytical Information Markup Language) - Standard ASTM E1947

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Xml.Linq;

namespace TecanSiLA2Server.AnIML
{
    /// <summary>
    /// Convertitore da formato XML Tecan nativo a AnIML standard.
    /// AnIML è lo standard ASTM E1947 per lo scambio di dati analitici.
    /// </summary>
    public class TecanToAnIMLConverter
    {
        // Namespace AnIML
        private static readonly XNamespace AnIML = "urn:org:astm:animl:schema:core:draft:0.90";
        private static readonly XNamespace SampleDoc = "urn:org:astm:animl:schema:technique:spectroscopy:draft:0.90";

        private readonly string _sampleSetId;
        private readonly string _plateId;
        private readonly DateTime _timestamp;

        public TecanToAnIMLConverter(string sampleSetId, string plateId)
        {
            _sampleSetId = sampleSetId ?? Guid.NewGuid().ToString();
            _plateId = plateId ?? "Unknown";
            _timestamp = DateTime.UtcNow;
        }

        /// <summary>
        /// Converte un file XML Tecan in formato AnIML
        /// </summary>
        /// <param name="tecanXmlPath">Percorso del file XML Tecan</param>
        /// <returns>Documento AnIML come XDocument</returns>
        public XDocument ConvertFromFile(string tecanXmlPath)
        {
            var tecanDoc = XDocument.Load(tecanXmlPath);
            return Convert(tecanDoc);
        }

        /// <summary>
        /// Converte un documento XML Tecan in formato AnIML
        /// </summary>
        /// <param name="tecanXml">Documento XML Tecan</param>
        /// <returns>Documento AnIML come XDocument</returns>
        public XDocument Convert(XDocument tecanXml)
        {
            var root = tecanXml.Root;
            if (root == null || root.Name.LocalName != "MeasurementResultData")
                throw new ArgumentException("Invalid Tecan XML format: Root element must be MeasurementResultData");

            // Estrai informazioni header
            var header = root.Element("Header");
            var instrument = header?.Element("Instrument");
            var parameters = header?.Element("Parameters");

            var instrumentInfo = new InstrumentInfo
            {
                Name = instrument?.Element("Name")?.Value ?? "Tecan Plate Reader",
                SerialNumber = instrument?.Element("SerialNumber")?.Value ?? "Unknown",
                FirmwareVersions = ExtractFirmwareVersions(header)
            };

            var plateInfo = parameters?.Elements("Parameter")
                .FirstOrDefault(p => p.Attribute("Name")?.Value == "Plate")?
                .Attribute("Value")?.Value ?? "Unknown";

            // Crea documento AnIML
            var animlDoc = CreateAnIMLDocument(instrumentInfo, plateInfo);

            // Processa ogni sezione di misurazione
            var sections = root.Elements("Section").ToList();
            int sampleIndex = 0;

            foreach (var section in sections)
            {
                ProcessSection(animlDoc, section, ref sampleIndex);
            }

            return animlDoc;
        }

        private XDocument CreateAnIMLDocument(InstrumentInfo instrumentInfo, string plateInfo)
        {
            var doc = new XDocument(
                new XDeclaration("1.0", "UTF-8", null),
                new XElement(AnIML + "AnIML",
                    new XAttribute("version", "0.90"),
                    new XAttribute(XNamespace.Xmlns + "animl", AnIML.NamespaceName),
                    
                    // Sample Set
                    new XElement(AnIML + "SampleSet",
                        new XAttribute("id", _sampleSetId),
                        new XAttribute("name", $"Plate {_plateId}"),
                        
                        // Plate Information as Sample Container
                        new XElement(AnIML + "Container",
                            new XAttribute("id", $"container_{_plateId}"),
                            new XAttribute("name", _plateId),
                            new XAttribute("containerType", "MicrotiterPlate"),
                            new XElement(AnIML + "Parameter",
                                new XAttribute("name", "PlateType"),
                                new XAttribute("parameterType", "String"),
                                new XElement(AnIML + "S", plateInfo)
                            )
                        )
                    ),
                    
                    // Experiment Step Set
                    new XElement(AnIML + "ExperimentStepSet",
                        new XAttribute("id", "experiments_" + _sampleSetId)
                    ),
                    
                    // Audit Trail
                    new XElement(AnIML + "AuditTrailEntrySet",
                        new XElement(AnIML + "AuditTrailEntry",
                            new XAttribute("id", "audit_1"),
                            new XElement(AnIML + "Timestamp", _timestamp.ToString("o")),
                            new XElement(AnIML + "Author",
                                new XAttribute("userType", "human"),
                                new XElement(AnIML + "Name", Environment.UserName)
                            ),
                            new XElement(AnIML + "Action", "created"),
                            new XElement(AnIML + "Comment", "Converted from Tecan XML format")
                        )
                    ),
                    
                    // Instrument Info in Signature
                    new XElement(AnIML + "SignatureSet",
                        new XElement(AnIML + "Signature",
                            new XAttribute("id", "instrument_signature"),
                            new XElement(AnIML + "Device",
                                new XAttribute("deviceIdentifier", instrumentInfo.SerialNumber),
                                new XElement(AnIML + "Name", instrumentInfo.Name),
                                new XElement(AnIML + "SerialNumber", instrumentInfo.SerialNumber),
                                new XElement(AnIML + "FirmwareVersion", 
                                    string.Join("; ", instrumentInfo.FirmwareVersions.Take(3)))
                            )
                        )
                    )
                )
            );

            return doc;
        }

        private void ProcessSection(XDocument animlDoc, XElement section, ref int sampleIndex)
        {
            var sectionName = section.Attribute("Name")?.Value ?? "Unknown";
            var startTime = ParseTecanDate(section.Attribute("Time_Start")?.Value);
            var parameters = section.Element("Parameters");

            // Determina il tipo di misurazione
            var mode = parameters?.Elements("Parameter")
                .FirstOrDefault(p => p.Attribute("Name")?.Value == "Mode")?
                .Attribute("Value")?.Value ?? "Unknown";

            var measurementType = DetermineMeasurementType(mode);

            // Estrai parametri della misurazione
            var measurementParams = ExtractMeasurementParameters(parameters);

            // Crea ExperimentStep per questa sezione
            var experimentStepSet = animlDoc.Root!.Element(AnIML + "ExperimentStepSet");
            var experimentStep = new XElement(AnIML + "ExperimentStep",
                new XAttribute("id", $"step_{sectionName}"),
                new XAttribute("name", sectionName),
                
                // Technique
                new XElement(AnIML + "Technique",
                    new XAttribute("name", measurementType),
                    new XAttribute("uri", GetTechniqueUri(measurementType))
                ),
                
                // Infrastructure
                new XElement(AnIML + "Infrastructure",
                    new XElement(AnIML + "Timestamp", startTime.ToString("o"))
                ),
                
                // Method
                new XElement(AnIML + "Method",
                    new XAttribute("name", $"{measurementType} Measurement"),
                    CreateMethodParameters(measurementParams)
                ),
                
                // Result Set
                new XElement(AnIML + "ResultSet",
                    new XAttribute("id", $"results_{sectionName}")
                )
            );

            experimentStepSet!.Add(experimentStep);

            // Processa i dati dei well
            var dataNode = section.Element("Data");
            var wells = dataNode?.Elements("Well").ToList() ?? new List<XElement>();
            var resultSet = experimentStep.Element(AnIML + "ResultSet");

            // Crea Serie per i risultati
            var seriesSet = new XElement(AnIML + "SeriesSet",
                new XAttribute("id", $"series_{sectionName}"),
                new XAttribute("name", "Well Readings"),
                new XAttribute("length", wells.Count.ToString())
            );

            // Serie per posizioni well
            var wellPositionSeries = new XElement(AnIML + "Series",
                new XAttribute("id", $"wellpos_{sectionName}"),
                new XAttribute("name", "Well Position"),
                new XAttribute("dependency", "independent"),
                new XAttribute("seriesType", "String"),
                new XAttribute("plotScale", "none"),
                new XElement(AnIML + "EncodedValueSet",
                    new XAttribute("startIndex", "0"),
                    new XAttribute("endIndex", (wells.Count - 1).ToString())
                )
            );

            // Serie per valori di misurazione
            var measurementSeries = new XElement(AnIML + "Series",
                new XAttribute("id", $"values_{sectionName}"),
                new XAttribute("name", GetSeriesName(measurementType)),
                new XAttribute("dependency", "dependent"),
                new XAttribute("seriesType", "Float64"),
                new XAttribute("plotScale", "linear"),
                new XElement(AnIML + "Unit",
                    new XAttribute("label", GetUnitLabel(measurementType))
                ),
                new XElement(AnIML + "EncodedValueSet",
                    new XAttribute("startIndex", "0"),
                    new XAttribute("endIndex", (wells.Count - 1).ToString())
                )
            );

            // Popola i dati
            var wellPositions = new List<string>();
            var measurements = new List<double>();
            // Per-well full spectrum, only populated for Type="Scan" wells (multiple
            // <Scan WL="..."> children instead of one <Single> value) - kept alongside
            // the scalar `measurements` series below (never in place of it) so every
            // existing consumer of that series keeps working unchanged.
            var spectra = new List<(string Pos, List<(int Wl, double Value)> Points)>();

            foreach (var well in wells)
            {
                var pos = well.Attribute("Pos")?.Value ?? "Unknown";
                var wellType = well.Attribute("Type")?.Value ?? "Single";
                wellPositions.Add(pos);

                if (wellType == "Scan")
                {
                    // Multi-wavelength scan: the well carries one <Scan WL="230">value</Scan>
                    // child per wavelength instead of a single <Single> value - reading only
                    // <Single> here (as before this fix) finds nothing and silently produces
                    // "0" for every well, discarding real data already present in the source XML.
                    var points = new List<(int, double)>();
                    foreach (var scanNode in well.Elements("Scan"))
                    {
                        var wlAttr = scanNode.Attribute("WL")?.Value;
                        if (!int.TryParse(wlAttr, NumberStyles.Any, CultureInfo.InvariantCulture, out var wl))
                            continue;
                        var scanValue = scanNode.Value;
                        double pointVal;
                        if (scanValue == "OVER")
                            pointVal = double.NaN;
                        else if (!double.TryParse(scanValue, NumberStyles.Any, CultureInfo.InvariantCulture, out pointVal))
                            pointVal = double.NaN;
                        points.Add((wl, pointVal));
                    }
                    spectra.Add((pos, points));

                    var finiteVals = points.Select(p => p.Item2).Where(v => !double.IsNaN(v)).ToList();
                    measurements.Add(finiteVals.Any() ? finiteVals.Average() : double.NaN);
                }
                else
                {
                    var singleNode = well.Element("Single");
                    var value = singleNode?.Value ?? "0";

                    if (value == "OVER")
                    {
                        measurements.Add(double.NaN); // Overflow
                    }
                    else if (double.TryParse(value, NumberStyles.Any, CultureInfo.InvariantCulture, out var dVal))
                    {
                        measurements.Add(dVal);
                    }
                    else
                    {
                        measurements.Add(0);
                    }
                }

                // Aggiungi anche come Sample individuale
                AddSampleToSampleSet(animlDoc, pos, sampleIndex++);
            }

            // Aggiungi valori encoded (base64 per efficienza con grandi dataset)
            var wellPosEncodedValue = wellPositionSeries.Element(AnIML + "EncodedValueSet");
            wellPosEncodedValue!.Add(new XElement(AnIML + "IndividualValueSet",
                wellPositions.Select(p => new XElement(AnIML + "S", p))
            ));

            var measurementEncodedValue = measurementSeries.Element(AnIML + "EncodedValueSet");
            measurementEncodedValue!.Add(new XElement(AnIML + "IndividualValueSet",
                measurements.Select(m => new XElement(AnIML + "F", 
                    double.IsNaN(m) ? "NaN" : m.ToString(CultureInfo.InvariantCulture)))
            ));

            // A well with a full spectrum only contributes its mean to `measurements`
            // above (kept for backward compatibility with the heatmap/statistics that
            // already read this series) - relabel it honestly instead of leaving it
            // looking like a single-point reading.
            if (spectra.Any(s => s.Points.Count > 0))
            {
                measurementSeries.SetAttributeValue("name", GetSeriesName(measurementType) + " (scan mean)");
            }

            seriesSet.Add(wellPositionSeries);
            seriesSet.Add(measurementSeries);
            resultSet!.Add(seriesSet);

            // Full per-well spectra for Type="Scan" wells - the actual data a
            // multi-wavelength scan produces, lost entirely before this fix. Kept as
            // a separate Result so nothing reading the SeriesSet above needs to change.
            if (spectra.Any(s => s.Points.Count > 0))
            {
                var wlStart = measurementParams.TryGetValue("Wavelength Start", out var wlStartStr)
                    && double.TryParse(wlStartStr, NumberStyles.Any, CultureInfo.InvariantCulture, out var wlStartVal) ? (double?)wlStartVal : null;
                var wlEnd = measurementParams.TryGetValue("Wavelength End", out var wlEndStr)
                    && double.TryParse(wlEndStr, NumberStyles.Any, CultureInfo.InvariantCulture, out var wlEndVal) ? (double?)wlEndVal : null;
                var wlStep = measurementParams.TryGetValue("Wavelength Step Size", out var wlStepStr)
                    && double.TryParse(wlStepStr, NumberStyles.Any, CultureInfo.InvariantCulture, out var wlStepVal) ? (double?)wlStepVal : null;

                var spectraResult = new XElement(AnIML + "Result",
                    new XAttribute("id", $"spectra_{sectionName}"),
                    new XAttribute("name", "Wavelength Scan Data")
                );
                if (wlStart.HasValue) spectraResult.Add(new XAttribute("wavelengthStartNm", wlStart.Value));
                if (wlEnd.HasValue) spectraResult.Add(new XAttribute("wavelengthEndNm", wlEnd.Value));
                if (wlStep.HasValue) spectraResult.Add(new XAttribute("wavelengthStepNm", wlStep.Value));

                foreach (var (pos, points) in spectra)
                {
                    if (points.Count == 0) continue;
                    spectraResult.Add(new XElement(AnIML + "SampleReference",
                        new XAttribute("sampleID", pos),
                        points.Select(p => new XElement(AnIML + "F",
                            new XAttribute("wl", p.Wl),
                            double.IsNaN(p.Value) ? "NaN" : p.Value.ToString(CultureInfo.InvariantCulture)))
                    ));
                }

                resultSet!.Add(spectraResult);
            }

            // Aggiungi statistiche
            AddStatistics(resultSet, measurements.Where(m => !double.IsNaN(m)).ToList(), measurementType);
        }

        private void AddSampleToSampleSet(XDocument animlDoc, string wellPosition, int index)
        {
            var sampleSet = animlDoc.Root!.Element(AnIML + "SampleSet");
            var container = sampleSet!.Element(AnIML + "Container");

            container!.Add(new XElement(AnIML + "Sample",
                new XAttribute("id", $"sample_{_plateId}_{wellPosition}"),
                new XAttribute("name", $"Sample at {wellPosition}"),
                new XAttribute("sampleID", $"{_plateId}_{wellPosition}"),
                new XElement(AnIML + "LocationInContainer",
                    new XAttribute("column", GetColumnFromWell(wellPosition)),
                    new XAttribute("row", GetRowFromWell(wellPosition))
                )
            ));
        }

        private void AddStatistics(XElement resultSet, List<double> values, string measurementType)
        {
            if (!values.Any()) return;

            var stats = new XElement(AnIML + "Result",
                new XAttribute("id", "statistics"),
                new XAttribute("name", "Summary Statistics"),
                new XElement(AnIML + "Parameter",
                    new XAttribute("name", "Count"),
                    new XAttribute("parameterType", "Int32"),
                    new XElement(AnIML + "I", values.Count)
                ),
                new XElement(AnIML + "Parameter",
                    new XAttribute("name", "Mean"),
                    new XAttribute("parameterType", "Float64"),
                    new XElement(AnIML + "F", values.Average().ToString(CultureInfo.InvariantCulture))
                ),
                new XElement(AnIML + "Parameter",
                    new XAttribute("name", "StandardDeviation"),
                    new XAttribute("parameterType", "Float64"),
                    new XElement(AnIML + "F", CalculateStdDev(values).ToString(CultureInfo.InvariantCulture))
                ),
                new XElement(AnIML + "Parameter",
                    new XAttribute("name", "Minimum"),
                    new XAttribute("parameterType", "Float64"),
                    new XElement(AnIML + "F", values.Min().ToString(CultureInfo.InvariantCulture))
                ),
                new XElement(AnIML + "Parameter",
                    new XAttribute("name", "Maximum"),
                    new XAttribute("parameterType", "Float64"),
                    new XElement(AnIML + "F", values.Max().ToString(CultureInfo.InvariantCulture))
                )
            );

            resultSet.Add(stats);
        }

        #region Helper Methods

        private List<string> ExtractFirmwareVersions(XElement? header)
        {
            var versions = new List<string>();
            var hwVersions = header?.Element("Hardware_Versions");
            if (hwVersions != null)
            {
                versions.AddRange(hwVersions.Elements("Version").Select(v => v.Value));
            }
            return versions;
        }

        private Dictionary<string, string> ExtractMeasurementParameters(XElement? parameters)
        {
            var dict = new Dictionary<string, string>();
            if (parameters == null) return dict;

            foreach (var param in parameters.Elements("Parameter"))
            {
                var name = param.Attribute("Name")?.Value;
                var value = param.Attribute("Value")?.Value;
                if (!string.IsNullOrEmpty(name) && value != null)
                {
                    dict[name!] = value;
                }
            }
            return dict;
        }

        private XElement CreateMethodParameters(Dictionary<string, string> parameters)
        {
            var category = new XElement(AnIML + "Category",
                new XAttribute("name", "Measurement Settings")
            );

            foreach (var kvp in parameters)
            {
                category.Add(new XElement(AnIML + "Parameter",
                    new XAttribute("name", kvp.Key),
                    new XAttribute("parameterType", "String"),
                    new XElement(AnIML + "S", kvp.Value)
                ));
            }

            return category;
        }

        private string DetermineMeasurementType(string mode)
        {
            var modeLower = mode.ToLowerInvariant();
            if (modeLower.Contains("absorbance") || modeLower.Contains("abs"))
                return "Absorbance";
            if (modeLower.Contains("fluorescence") || modeLower.Contains("flu"))
                return "Fluorescence";
            if (modeLower.Contains("luminescence") || modeLower.Contains("lum"))
                return "Luminescence";
            return "Spectroscopy";
        }

        private string GetTechniqueUri(string measurementType)
        {
            return measurementType switch
            {
                "Absorbance" => "urn:astm:animl:technique:spectroscopy:absorbance",
                "Fluorescence" => "urn:astm:animl:technique:spectroscopy:fluorescence",
                "Luminescence" => "urn:astm:animl:technique:spectroscopy:luminescence",
                _ => "urn:astm:animl:technique:spectroscopy"
            };
        }

        private string GetSeriesName(string measurementType)
        {
            return measurementType switch
            {
                "Absorbance" => "Optical Density",
                "Fluorescence" => "Relative Fluorescence Units",
                "Luminescence" => "Relative Light Units",
                _ => "Measurement Value"
            };
        }

        private string GetUnitLabel(string measurementType)
        {
            return measurementType switch
            {
                "Absorbance" => "OD",
                "Fluorescence" => "RFU",
                "Luminescence" => "RLU",
                _ => "AU"
            };
        }

        private DateTime ParseTecanDate(string? dateStr)
        {
            if (string.IsNullOrEmpty(dateStr))
                return DateTime.UtcNow;

            if (DateTime.TryParse(dateStr, CultureInfo.InvariantCulture, 
                DateTimeStyles.AssumeUniversal, out var dt))
                return dt;

            return DateTime.UtcNow;
        }

        private int GetColumnFromWell(string wellPos)
        {
            // A1 -> colonna 1, A12 -> colonna 12
            var numPart = new string(wellPos.Where(char.IsDigit).ToArray());
            return int.TryParse(numPart, out var col) ? col : 1;
        }

        private int GetRowFromWell(string wellPos)
        {
            // A1 -> riga 1 (A), H1 -> riga 8 (H)
            var letterPart = wellPos.FirstOrDefault(char.IsLetter);
            return char.ToUpper(letterPart) - 'A' + 1;
        }

        private double CalculateStdDev(List<double> values)
        {
            if (values.Count < 2) return 0;
            var avg = values.Average();
            var sumSq = values.Sum(v => Math.Pow(v - avg, 2));
            return Math.Sqrt(sumSq / (values.Count - 1));
        }

        #endregion

        #region Data Classes

        private class InstrumentInfo
        {
            public string Name { get; set; } = "";
            public string SerialNumber { get; set; } = "";
            public List<string> FirmwareVersions { get; set; } = new();
        }

        #endregion
    }

    /// <summary>
    /// Extension methods per lavorare con AnIML
    /// </summary>
    public static class AnIMLExtensions
    {
        /// <summary>
        /// Salva il documento AnIML su file
        /// </summary>
        public static void SaveToFile(this XDocument animlDoc, string filePath)
        {
            animlDoc.Save(filePath);
        }

        /// <summary>
        /// Converte il documento AnIML in array di byte (per trasmissione SiLA2)
        /// </summary>
        public static byte[] ToByteArray(this XDocument animlDoc)
        {
            using var ms = new MemoryStream();
            animlDoc.Save(ms);
            return ms.ToArray();
        }

        /// <summary>
        /// Converte il documento AnIML in stringa XML
        /// </summary>
        public static string ToXmlString(this XDocument animlDoc)
        {
            return animlDoc.ToString(SaveOptions.None);
        }
    }
}
