# Application de test WPF (.NET Framework, Windows PowerShell 5.1) affichée sans activation (ShowActivated=False).
# Usage : powershell.exe -NoProfile -STA -File wpf_app.ps1 [-X 180] [-Y 180]
param([int]$X = 180, [int]$Y = 180)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase
[xml]$xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Labo WPF" Width="580" Height="660" ShowActivated="False" WindowStartupLocation="Manual">
  <DockPanel>
    <Menu DockPanel.Dock="Top">
      <MenuItem Header="Fichier"><MenuItem x:Name="Action" Header="Action menu"/></MenuItem>
    </Menu>
    <Grid Margin="10">
      <Grid.ColumnDefinitions><ColumnDefinition Width="90"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
      <Grid.RowDefinitions>
        <RowDefinition Height="32"/><RowDefinition Height="70"/><RowDefinition Height="40"/><RowDefinition Height="34"/>
        <RowDefinition Height="74"/><RowDefinition Height="32"/><RowDefinition Height="30"/><RowDefinition Height="*"/>
        <RowDefinition Height="96"/>
      </Grid.RowDefinitions>
      <Label Content="Nom" Target="{Binding ElementName=Nom}"/>
      <TextBox x:Name="Nom" Grid.Column="1" Margin="0,3" AutomationProperties.Name="Nom"/>
      <Label Grid.Row="1" Content="Notes"/>
      <TextBox x:Name="Notes" Grid.Row="1" Grid.Column="1" Margin="0,3" AcceptsReturn="True" TextWrapping="Wrap" AutomationProperties.Name="Notes"/>
      <StackPanel Grid.Row="2" Grid.Column="1" Orientation="Horizontal">
        <Button x:Name="Valider" Content="Valider" Width="120" Margin="0,6"/>
        <CheckBox x:Name="Option" Content="Option" Margin="20,10"/>
        <Button x:Name="Dialogue" Content="Dialogue" Width="120" Margin="10,6"/>
      </StackPanel>
      <Label Grid.Row="3" Content="Couleur"/>
      <ComboBox x:Name="Couleur" Grid.Row="3" Grid.Column="1" Width="200" HorizontalAlignment="Left" Margin="0,4" AutomationProperties.Name="Couleur">
        <ComboBoxItem Content="Rouge"/><ComboBoxItem Content="Vert"/><ComboBoxItem Content="Bleu"/>
      </ComboBox>
      <Label Grid.Row="4" Content="Liste"/>
      <ListBox x:Name="Liste" Grid.Row="4" Grid.Column="1" Width="200" HorizontalAlignment="Left" Margin="0,3" AutomationProperties.Name="Liste">
        <ListBoxItem Content="Alpha"/><ListBoxItem Content="Bravo"/><ListBoxItem Content="Charlie"/>
        <ListBox.ContextMenu><ContextMenu><MenuItem x:Name="Copier" Header="Copier"/></ContextMenu></ListBox.ContextMenu>
      </ListBox>
      <Label Grid.Row="5" Content="Lecture"/>
      <TextBox Grid.Row="5" Grid.Column="1" Width="200" HorizontalAlignment="Left" Margin="0,3" IsReadOnly="True" Text="abc" AutomationProperties.Name="Lecture"/>
      <Label Grid.Row="6" Content="Statut"/>
      <TextBlock x:Name="Statut" Grid.Row="6" Grid.Column="1" Margin="0,6" Text="prêt" AutomationProperties.Name="Statut"/>
      <Border x:Name="Zone" Grid.Row="7" Grid.Column="1" BorderBrush="Gray" BorderThickness="1" Background="White" AutomationProperties.Name="Zone"/>
      <!-- Vraie zone à défilement (ScrollPattern UIA) : le banc y prouve une molette par la position relue. -->
      <Label Grid.Row="8" Content="Défilement"/>
      <!-- Deux ScrollViewer imbriqués de même taille : l'intérieur (« Défilement », visé par le banc) remplit la vue de
           l'extérieur ; seul l'intérieur doit bouger (relecture de Codex : un outil qui fait défiler le mauvais
           conteneur se voit). -->
      <ScrollViewer x:Name="DefilExt" Grid.Row="8" Grid.Column="1" Margin="0,4" Height="88" VerticalScrollBarVisibility="Visible" CanContentScroll="False" AutomationProperties.Name="Défilement extérieur">
        <StackPanel x:Name="DefilExtContenu">
          <ScrollViewer x:Name="Defil" Height="80" VerticalScrollBarVisibility="Visible" HorizontalScrollBarVisibility="Auto" CanContentScroll="False" AutomationProperties.Name="Défilement">
            <StackPanel x:Name="DefilContenu"/>
          </ScrollViewer>
        </StackPanel>
      </ScrollViewer>
    </Grid>
  </DockPanel>
</Window>
'@
$window = [Windows.Markup.XamlReader]::Load((New-Object System.Xml.XmlNodeReader $xaml))
# Affichée hors écran, mise au BAS de l'empilement, puis seulement placée (une fenêtre montrée sans activation se pose
# quand même en haut : elle passait un instant par-dessus les fenêtres de Julien, banc du 25/10).
$window.Left = -32000; $window.Top = -32000
Add-Type -Namespace Labo -Name Z -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);' 
$find = { param($n) $window.FindName($n) }
$statut = & $find 'Statut'
$script:clics = 0
function Set-Statut([string]$t) { $statut.Text = $t; [Console]::WriteLine('{"event":"' + $t.Replace('"', "'") + '"}') }
(& $find 'Valider').Add_Click({ $script:clics++; Set-Statut "clic $script:clics" })
(& $find 'Action').Add_Click({ Set-Statut 'menu action' })
$opt = & $find 'Option'
$opt.Add_Checked({ Set-Statut 'coche on' }); $opt.Add_Unchecked({ Set-Statut 'coche off' })
$cb = & $find 'Couleur'; $cb.Add_SelectionChanged({ Set-Statut ("couleur " + $cb.SelectedItem.Content) })
$lb = & $find 'Liste'; $lb.Add_SelectionChanged({ Set-Statut ("liste " + $lb.SelectedItem.Content) })
$lb.Add_MouseDoubleClick({ Set-Statut ("double " + $lb.SelectedItem.Content) })
$lb.ContextMenu.Items[0].Add_Click({ Set-Statut 'contexte copier' })
# Vraie boîte modale (ShowDialog, propriétaire = la fenêtre du labo), affichée sans activation comme le labo.
(& $find 'Dialogue').Add_Click({
    Set-Statut 'modale ouverte'
    $script:modale = 'modale fermée'
    $box = New-Object System.Windows.Window -Property @{ Title = 'Confirmation'; Width = 280; Height = 130; ShowActivated = $false;
        Owner = $window; WindowStartupLocation = 'CenterOwner'; ResizeMode = 'NoResize' }
    $panel = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; Margin = '20' }
    $ok = New-Object System.Windows.Controls.Button -Property @{ Content = 'OK'; Width = 80; Margin = '5' }
    $cancel = New-Object System.Windows.Controls.Button -Property @{ Content = 'Annuler'; Width = 80; Margin = '5' }
    $ok.Add_Click({ $script:modale = 'modale ok'; $box.Close() }); $cancel.Add_Click({ $script:modale = 'modale annulée'; $box.Close() })
    [void]$panel.Children.Add($ok); [void]$panel.Children.Add($cancel); $box.Content = $panel
    [void]$box.ShowDialog()
    Set-Statut $script:modale
})
$zone = & $find 'Zone'
$zone.Add_MouseDown({ param($s, $e) $p = $e.GetPosition($zone); $b = if ($e.ChangedButton -eq 'Left') { 'gauche' } else { 'droit' }; Set-Statut ("zone $b {0},{1}" -f [int]$p.X, [int]$p.Y) })
$zone.Add_MouseWheel({ param($s, $e) Set-Statut ("molette " + $e.Delta) })
# Défilement : un évènement par changement de position verticale de CHAQUE ScrollViewer (A = intérieur, visé ; B =
# extérieur), avec le sens, l'amplitude et la position. ScrollChanged remonte (routé) : seul l'émetteur réel compte.
# La construction du contenu ne bouge aucune position, donc n'écrit rien.
$contenu = & $find 'DefilContenu'
# Lignes plus larges que la zone : elle défile aussi en X (preuve B des quatre sens, 22/10) ; l'extérieur reste vertical.
1..400 | ForEach-Object { [void]$contenu.Children.Add((New-Object System.Windows.Controls.TextBlock -Property @{ Text = "Ligne $_"; Width = 600 })) }
$contenuExt = & $find 'DefilExtContenu'
1..200 | ForEach-Object { [void]$contenuExt.Children.Add((New-Object System.Windows.Controls.TextBlock -Property @{ Text = "Extérieur $_" })) }
$defil = & $find 'Defil'; $defilExt = & $find 'DefilExt'
foreach ($pair in @(@($defil, 'A'), @($defilExt, 'B'))) {
    $viewer = $pair[0]; $nom = $pair[1]
    $viewer.Add_ScrollChanged({ param($s, $e)
        if ($e.OriginalSource -ne $s) { return }
        # Horizontal (preuve des quatre sens) : « defilx A droite <amplitude> <position> », hors de la famille « defil ».
        if ($e.HorizontalChange -ne 0) {
            $sensX = if ($e.HorizontalChange -gt 0) { 'droite' } else { 'gauche' }
            Set-Statut ("defilx {0} $sensX {1} {2}" -f $s.Tag, [int][math]::Round([math]::Abs($e.HorizontalChange)), [int][math]::Round($e.HorizontalOffset))
        }
        if ($e.VerticalChange -eq 0) { return }
        $sens = if ($e.VerticalChange -gt 0) { 'bas' } else { 'haut' }
        Set-Statut ("defil {0} $sens {1} {2}" -f $s.Tag, [int][math]::Round([math]::Abs($e.VerticalChange)), [int][math]::Round($e.VerticalOffset))
    })
    $viewer.Tag = $nom
}
$window.Add_KeyDown({ param($s, $e) if (($e.Key -eq 'K') -and ([System.Windows.Input.Keyboard]::Modifiers -band [System.Windows.Input.ModifierKeys]::Control)) { Set-Statut 'raccourci ctrl+k'; $e.Handled = $true } })
$window.Add_ContentRendered({
    $h = (New-Object System.Windows.Interop.WindowInteropHelper $window).Handle
    [void][Labo.Z]::SetWindowPos($h, [IntPtr]1, 0, 0, 0, 0, 0x13)  # HWND_BOTTOM, NOSIZE|NOMOVE|NOACTIVATE
    $window.Left = $X; $window.Top = $Y
    # Lignes par cran de molette (réglage Windows) : le banc en déduit le déplacement d'un cran, jamais supposé à 3.
    [Console]::WriteLine('{"ready":true,"top":' + $h.ToInt64() + ',"wheel_lines":' + [System.Windows.SystemParameters]::WheelScrollLines + '}')
})
[void]$window.ShowDialog()
