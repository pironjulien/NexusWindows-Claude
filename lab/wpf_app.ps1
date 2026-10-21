# Application de test WPF (.NET Framework, Windows PowerShell 5.1) affichée sans activation (ShowActivated=False).
# Usage : powershell.exe -NoProfile -STA -File wpf_app.ps1 [-X 180] [-Y 180]
param([int]$X = 180, [int]$Y = 180)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase
[xml]$xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Labo WPF" Width="580" Height="560" ShowActivated="False" WindowStartupLocation="Manual">
  <DockPanel>
    <Menu DockPanel.Dock="Top">
      <MenuItem Header="Fichier"><MenuItem x:Name="Action" Header="Action menu"/></MenuItem>
    </Menu>
    <Grid Margin="10">
      <Grid.ColumnDefinitions><ColumnDefinition Width="90"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
      <Grid.RowDefinitions>
        <RowDefinition Height="32"/><RowDefinition Height="70"/><RowDefinition Height="40"/><RowDefinition Height="34"/>
        <RowDefinition Height="74"/><RowDefinition Height="32"/><RowDefinition Height="30"/><RowDefinition Height="*"/>
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
    </Grid>
  </DockPanel>
</Window>
'@
$window = [Windows.Markup.XamlReader]::Load((New-Object System.Xml.XmlNodeReader $xaml))
$window.Left = $X; $window.Top = $Y
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
$window.Add_KeyDown({ param($s, $e) if (($e.Key -eq 'K') -and ([System.Windows.Input.Keyboard]::Modifiers -band [System.Windows.Input.ModifierKeys]::Control)) { Set-Statut 'raccourci ctrl+k'; $e.Handled = $true } })
$window.Add_ContentRendered({
    $h = (New-Object System.Windows.Interop.WindowInteropHelper $window).Handle
    [Console]::WriteLine('{"ready":true,"top":' + $h.ToInt64() + '}')
})
[void]$window.ShowDialog()
