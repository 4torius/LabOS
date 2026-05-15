// ResultParser.cs
// Parser per convertire i risultati XML Tecan in formato CSV
// Adattato da iControlBridge per il server SiLA2

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text.RegularExpressions;
using System.Xml;

namespace TecanSiLA2Server.Instrument
{
    /// <summary>
    /// Parser per convertire i risultati XML Tecan in formato CSV compatibile con i-control
    /// </summary>
    public class ResultParser
    {
        private readonly XmlDocument _doc;
        private readonly string _serial;
        private readonly string _device;
        private readonly string _temp;

        /// <summary>
        /// Costruttore
        /// </summary>
        /// <param name="xmlFilePath">Percorso del file XML dei risultati Tecan</param>
        /// <param name="serial">Numero seriale dello strumento</param>
        /// <param name="device">Nome del dispositivo</param>
        /// <param name="temp">Temperatura attuale (es. "37.0")</param>
        public ResultParser(string xmlFilePath, string serial, string device, string temp)
        {
            _doc = new XmlDocument();
            _doc.Load(xmlFilePath);
            _serial = serial ?? "N/A";
            _device = device ?? "N/A";
            _temp = temp ?? "N/A";
        }

        /// <summary>
        /// Genera il contenuto CSV in formato i-control
        /// </summary>
        public List<string> GetResultsAsIcontrolCsv()
        {
            var res = new List<string>();

            // --- 1. Estrai Nodi Intestazione Globale ---
            XmlNode? root = _doc.SelectSingleNode("/MeasurementResultData");
            XmlNode? header = _doc.SelectSingleNode("/MeasurementResultData/Header");
            XmlNode? scriptNode = _doc.SelectSingleNode("//Script");

            if (root == null || header == null || scriptNode == null)
            {
                res.Add("ERROR: Cannot find required XML nodes (Header or Script).");
                return res;
            }

            // --- LETTURA INTESTAZIONI GLOBALI ---
            string firmware = GetHeaderText(header, "Hardware_Versions/Version", "N/A");

            res.Add($"Application: Tecan i-control,,,,{GetHeaderText(header, "Application", "Tecan i-control")},,,,,,,,");
            res.Add($"Device: {_device},,,,Serial number: {_serial},,,,,,,,");
            res.Add($"Firmware: {firmware},,,,MAI, {firmware},,,,,,,,");
            res.Add(",,,,,,,,,,,,");

            // --- Ciclo Multi-Sezione ---
            XmlNodeList? allSections = _doc.SelectNodes("/MeasurementResultData/Section");
            if (allSections == null) return res;

            int sectionCount = 0;
            foreach (XmlNode section in allSections)
            {
                sectionCount++;
                XmlNode? parameters = section.SelectSingleNode("Parameters");
                if (parameters == null) continue;

                DateTime startTime = ParseTecanDate(GetAttribute(section, "Time_Start"));
                
                if (sectionCount > 1)
                {
                    res.Add(",,,,,,,,,,,,");
                    res.Add(",,,,,,,,,,,,");
                    res.Add(",,,,,,,,,,,,");
                    res.Add(",,,,,,,,,,,,");
                }

                // Solo la prima sezione stampa Data/Ora/Plate
                if (sectionCount == 1)
                {
                    res.Add($"Date:,{startTime:dd/MM/yyyy},,,,,,,,,,,");
                    res.Add($"Time:,{startTime:HH:mm:ss},,,,,,,,,,,");
                    res.Add(",,,,,,,,,,,,");
                    res.Add(",,,,,,,,,,,,");
                    
                    var headerParams = header.SelectSingleNode("Parameters");
                    res.Add($"Plate,,,,{GetParameterValue(headerParams, "Plate", "N/A")},,,,,,,,");
                    res.Add("Plate-ID (Stacker),,,,,,,,,,,,");
                    res.Add(",,,,,,,,,,,,");
                    res.Add(",,,,,,,,,,,,");
                }

                // --- LETTURA DINAMICA PARAMETRI DI SEZIONE ---
                string labelName = GetAttribute(section, "Name", "N/A");
                string modeName = GetParameterValue(parameters, "Mode", "N/A");

                res.Add($"Label: {labelName},,,,,,,,,,,,");
                res.Add($"Mode,,,,{modeName},,,,,,,,");

                // Aggiungi parametri specifici solo se esistono
                AddParameterIfExists(res, parameters, "Measurement Wavelength", "nm");
                AddParameterIfExists(res, parameters, "Excitation Wavelength", "nm");
                AddParameterIfExists(res, parameters, "Emission Wavelength", "nm");
                AddParameterIfExists(res, parameters, "Bandwidth", "nm");
                AddParameterIfExists(res, parameters, "Excitation Bandwidth", "nm");
                AddParameterIfExists(res, parameters, "Emission Bandwidth", "nm");
                AddParameterIfExists(res, parameters, "Number of Flashes", "");
                AddParameterIfExists(res, parameters, "Integration Time", "µs");
                AddParameterIfExists(res, parameters, "Settle Time", "ms");
                AddParameterIfExists(res, parameters, "Gain", "");

                res.Add($"Start Time:,{startTime:dd/MM/yyyy HH:mm:ss},,,,,,,,,,,");
                res.Add(",,,,,,,,,,,,");
                res.Add($",Temperature: {_temp} °C,,,,,,,,,,,");

                // --- 3. Cerca i nodi <Single> SOLO per questa sezione ---
                XmlNodeList? resultNodes = section.SelectNodes("Data/Well/Single");
                if (resultNodes == null || resultNodes.Count == 0)
                    continue;

                // --- 4. Costruisci la Matrice Dati in Memoria ---
                var dataMatrix = new Dictionary<string, string>();
                foreach (XmlNode node in resultNodes)
                {
                    if (node.ParentNode == null) continue;
                    
                    string wellPos = GetAttribute(node.ParentNode, "Pos");
                    string value = node.InnerText.Trim();

                    if (value != "OVER" && double.TryParse(value, NumberStyles.Any, CultureInfo.InvariantCulture, out double dVal))
                    {
                        value = dVal.ToString(CultureInfo.InvariantCulture);
                    }

                    if (!string.IsNullOrEmpty(wellPos) && wellPos != "N/A")
                    {
                        dataMatrix[wellPos] = value;
                    }
                }

                // --- 5. Scrivi la Matrice nel CSV (Griglia Adattiva) ---
                XmlNode? plateNode = scriptNode.SelectSingleNode("//*[local-name()='CyclePlate']");
                string plateFileName = GetAttribute(plateNode, "file");

                XmlNode? plateRangeNode = scriptNode.SelectSingleNode("//*[local-name()='PlateRange']");
                string rangeString = GetAttribute(plateRangeNode, "range");

                GetPlateDimensions(plateFileName, rangeString, out int numRows, out int numCols);

                // Intestazione Colonne
                var colHeader = new List<string> { "<>" };
                for (int c = 1; c <= numCols; c++)
                    colHeader.Add(c.ToString());
                res.Add(string.Join(",", colHeader));

                // Righe Dati
                for (int r = 0; r < numRows; r++)
                {
                    string rowLetter = ((char)('A' + r)).ToString();
                    var csvRow = new List<string> { rowLetter };

                    for (int c = 1; c <= numCols; c++)
                    {
                        string wellPos = rowLetter + c;
                        csvRow.Add(dataMatrix.TryGetValue(wellPos, out string? val) ? val : "");
                    }
                    res.Add(string.Join(",", csvRow));
                }

                // --- 6. Footer di Sezione ---
                DateTime endTime = ParseTecanDate(GetAttribute(section, "Time_End"));
                res.Add(",,,,,,,,,,,,");
                res.Add(",,,,,,,,,,,,");
                res.Add($"End Time:,{endTime:dd/MM/yyyy HH:mm:ss},,,,,,,,,,,");
            }

            return res;
        }

        /// <summary>
        /// Verifica se il documento contiene dati
        /// </summary>
        public bool HasData()
        {
            return _doc?.SelectSingleNode("//Single") != null;
        }

        #region Helper Methods

        private void AddParameterIfExists(List<string> res, XmlNode? paramsNode, string paramName, string unit)
        {
            string value = GetParameterValue(paramsNode, paramName, "N/A");
            if (value == "N/A") return;

            // Formatta i valori di wavelength/bandwidth (es. 2300 -> 230)
            if (paramName.ToLower().Contains("wavelength") || paramName.ToLower().Contains("bandwidth"))
            {
                if (double.TryParse(value, out double dVal))
                    value = (dVal / 10.0).ToString("0.#", CultureInfo.InvariantCulture);
            }

            string unitSuffix = string.IsNullOrEmpty(unit) ? "" : $",{unit}";
            res.Add($"{paramName},,,,{value}{unitSuffix},,,,,,,");
        }

        private void GetPlateDimensions(string plateFileName, string rangeString, out int rows, out int cols)
        {
            rows = 8;
            cols = 12;

            if (string.IsNullOrEmpty(rangeString) || rangeString == "N/A")
            {
                plateFileName = plateFileName?.ToLower() ?? "";
                if (plateFileName.Contains("384")) { rows = 16; cols = 24; }
                else if (plateFileName.Contains("96")) { rows = 8; cols = 12; }
                else if (plateFileName.Contains("48")) { rows = 6; cols = 8; }
                else if (plateFileName.Contains("24")) { rows = 4; cols = 6; }
                else if (plateFileName.Contains("12")) { rows = 3; cols = 4; }
                else if (plateFileName.Contains("6")) { rows = 2; cols = 3; }
                return;
            }

            try
            {
                string lastWell = rangeString.Split('~', ':')[1];
                Match m = Regex.Match(lastWell, @"([A-P])([0-9]+)");
                if (m.Success)
                {
                    rows = (m.Groups[1].Value[0] - 'A') + 1;
                    cols = int.Parse(m.Groups[2].Value);
                }
            }
            catch
            {
                rows = 8;
                cols = 12;
            }
        }

        private string GetHeaderText(XmlNode? headerNode, string nodeName, string defaultValue = "N/A")
        {
            if (headerNode == null) return defaultValue;
            XmlNode? childNode = headerNode.SelectSingleNode(nodeName);
            return childNode?.InnerText?.Trim() ?? defaultValue;
        }

        private string GetParameterValue(XmlNode? paramsNode, string paramName, string defaultValue = "N/A")
        {
            if (paramsNode == null) return defaultValue;
            XmlNode? paramNode = paramsNode.SelectSingleNode($"Parameter[@Name='{paramName}']");
            return paramNode != null ? GetAttribute(paramNode, "Value") : defaultValue;
        }

        private string GetAttribute(XmlNode? node, string attributeName)
        {
            return node?.Attributes?[attributeName]?.InnerText ?? "N/A";
        }

        private string GetAttribute(XmlNode? node, string attributeName, string defaultValue)
        {
            string val = GetAttribute(node, attributeName);
            return val == "N/A" ? defaultValue : val;
        }

        private DateTime ParseTecanDate(string tecanDateStr)
        {
            if (string.IsNullOrEmpty(tecanDateStr) || tecanDateStr == "N/A")
                return DateTime.Now;
            
            return DateTime.TryParse(tecanDateStr, null, DateTimeStyles.RoundtripKind, out DateTime result) 
                ? result 
                : DateTime.Now;
        }

        #endregion
    }
}
