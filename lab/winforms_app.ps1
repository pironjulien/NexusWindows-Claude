# Application de test WinForms (.NET Framework, Windows PowerShell 5.1) affichée sans activation.
# Usage : powershell.exe -NoProfile -STA -File winforms_app.ps1 [-X 160] [-Y 160]
param([int]$X = 160, [int]$Y = 160)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -ReferencedAssemblies System.Windows.Forms, System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Windows.Forms;

public class LabForm : Form {
    protected override bool ShowWithoutActivation { get { return true; } }
    Label status; int clicks;
    void Status(string text) { status.Text = text; Console.WriteLine("{\"event\":\"" + text.Replace("\"", "'") + "\"}"); }

    public LabForm(int x, int y) {
        Text = "Labo WinForms"; StartPosition = FormStartPosition.Manual; Location = new Point(x, y);
        ClientSize = new Size(560, 500); KeyPreview = true; Font = new Font("Segoe UI", 9f);
        var menu = new MenuStrip(); var file = new ToolStripMenuItem("Fichier");
        var action = new ToolStripMenuItem("Action menu"); action.Click += (s, e) => Status("menu action");
        file.DropDownItems.Add(action); menu.Items.Add(file); MainMenuStrip = menu; Controls.Add(menu);
        int top = 34;
        Func<string, int, Label> label = (t, yy) => { var l = new Label { Text = t, Location = new Point(10, yy + 3), AutoSize = true }; Controls.Add(l); return l; };
        label("Nom", top); var name = new TextBox { Name = "nom", Location = new Point(100, top), Width = 430, AccessibleName = "Nom" }; Controls.Add(name);
        label("Notes", top + 32); var notes = new TextBox { Name = "notes", Multiline = true, AcceptsReturn = true, Location = new Point(100, top + 32), Size = new Size(430, 60), AccessibleName = "Notes" }; Controls.Add(notes);
        var button = new Button { Text = "Valider", Location = new Point(100, top + 100), Width = 120 }; button.Click += (s, e) => Status("clic " + (++clicks)); Controls.Add(button);
        var check = new CheckBox { Text = "Option", Location = new Point(240, top + 102), AutoSize = true }; check.CheckedChanged += (s, e) => Status("coche " + (check.Checked ? "on" : "off")); Controls.Add(check);
        label("Couleur", top + 140); var combo = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Location = new Point(100, top + 140), Width = 200, AccessibleName = "Couleur" };
        combo.Items.AddRange(new object[] { "Rouge", "Vert", "Bleu" }); combo.SelectedIndexChanged += (s, e) => Status("couleur " + combo.SelectedItem); Controls.Add(combo);
        label("Liste", top + 176); var list = new ListBox { Location = new Point(100, top + 176), Size = new Size(200, 60), AccessibleName = "Liste" };
        list.Items.AddRange(new object[] { "Alpha", "Bravo", "Charlie" }); list.SelectedIndexChanged += (s, e) => Status("liste " + list.SelectedItem); Controls.Add(list);
        label("Lecture", top + 246); Controls.Add(new TextBox { ReadOnly = true, Text = "abc", Location = new Point(100, top + 246), Width = 200, AccessibleName = "Lecture" });
        label("Statut", top + 280); status = new Label { Name = "statut", Text = "prêt", Location = new Point(100, top + 283), AutoSize = true, AccessibleName = "Statut" }; Controls.Add(status);
        var zone = new Panel { Location = new Point(100, top + 310), Size = new Size(430, 110), BorderStyle = BorderStyle.FixedSingle, BackColor = Color.White, AccessibleName = "Zone" };
        zone.MouseDown += (s, e) => Status("zone " + (e.Button == MouseButtons.Left ? "gauche" : "droit") + " " + e.X + "," + e.Y);
        zone.MouseWheel += (s, e) => Status("molette " + e.Delta); Controls.Add(zone);
        KeyDown += (s, e) => { if (e.Control && e.KeyCode == Keys.K) { Status("raccourci ctrl+k"); e.Handled = true; e.SuppressKeyPress = true; } };
        Shown += (s, e) => { Console.WriteLine("{\"ready\":true,\"top\":" + Handle.ToInt64() + "}"); };
    }
}
'@
$form = New-Object LabForm -ArgumentList $X, $Y
[System.Windows.Forms.Application]::Run($form)
