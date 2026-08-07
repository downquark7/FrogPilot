#include <regex>

#include <QtConcurrent>

#include "frogpilot/ui/qt/offroad/maps_settings.h"
#include "frogpilot/ui/qt/widgets/frogpilot_controls.h"

FrogPilotMapsPanel::FrogPilotMapsPanel(FrogPilotSettingsWindow *parent) : FrogPilotListWidget(parent), parent(parent) {
  QJsonObject shownDescriptions = QJsonDocument::fromJson(QString::fromStdString(params.get("ShownToggleDescriptions")).toUtf8()).object();
  QString className = this->metaObject()->className();

  if (!shownDescriptions.value(className).toBool(false)) {
    forceOpenDescriptions = true;
    shownDescriptions.insert(className, true);
    params.put("ShownToggleDescriptions", QJsonDocument(shownDescriptions).toJson(QJsonDocument::Compact).toStdString());
  }

  QStackedLayout *mapsLayout = new QStackedLayout();
  addItem(mapsLayout);

  FrogPilotListWidget *settingsList = new FrogPilotListWidget(this);

  std::vector<QString> scheduleOptions{tr("Manually"), tr("Weekly"), tr("Monthly")};
  preferredSchedule = new ButtonParamControl("PreferredSchedule", tr("Automatically Update Maps"),
                                          tr("<b>How often openpilot re-downloads the speed limit map data for the places you picked under \"Map Sources\". \"Weekly\" runs every Sunday, \"Monthly\" runs on the 1st, and \"Manually\" waits until you press \"DOWNLOAD\" yourself.</b><br><br>"
                                             "There is one exception. Whenever the map data is missing from the device, openpilot starts the download on its own, usually within the hour, and that one is not held back until you park."),
                                             "",
                                             scheduleOptions);
  settingsList->addItem(preferredSchedule);

  downloadMapsButton = new ButtonControl(tr("Download Maps"), tr("DOWNLOAD"), tr("<b>Start downloading the speed limit map data for the places you picked under \"Map Sources\".</b><br><br>Your car has to be parked and online. Large areas can take hours and use several gigabytes."));
  QObject::connect(downloadMapsButton, &ButtonControl::clicked, [this] {
    if (downloadMapsButton->text() == tr("CANCEL")) {
      if (FrogPilotConfirmationDialog::yesorno(tr("Cancel the download?"), this)) {
        cancelDownload();
      }
    } else {
      startDownload();
    }
  });
  settingsList->addItem(downloadMapsButton);

  settingsList->addItem(lastMapsDownload = new LabelControl(tr("Last Updated"), params.get("LastMapsUpdate").empty() ? tr("Never") : QString::fromStdString(params.get("LastMapsUpdate"))));

  selectMaps = new FrogPilotButtonsControl(tr("Map Sources"),
                                           tr("<b>Pick the countries or U.S. states you drive in, so openpilot knows their speed limits.</b><br><br>Only what you pick here gets downloaded, so pick as little as covers your driving.") ,
                                              "", {tr("COUNTRIES"), tr("STATES")});
  QObject::connect(selectMaps, &FrogPilotButtonsControl::buttonClicked, [mapsLayout, this](int id) {
    mapsLayout->setCurrentIndex(id + 1);

    openSubPanel();
  });
  settingsList->addItem(selectMaps);

  settingsList->addItem(downloadStatus = new LabelControl(tr("Progress")));
  settingsList->addItem(downloadTimeElapsed = new LabelControl(tr("Time Elapsed")));
  settingsList->addItem(downloadETA = new LabelControl(tr("Time Remaining")));

  downloadETA->setVisible(false);
  downloadStatus->setVisible(false);
  downloadTimeElapsed->setVisible(false);

  removeMapsButton = new ButtonControl(tr("Remove Maps"), tr("REMOVE"), tr("<b>Delete your downloaded map data and clear the places you picked under \"Map Sources\", to free up storage.</b><br><br>Nothing comes back on its own, so \"Speed Limit Controller\" has no map speed limits until you pick your places again and start a new download."));
  QObject::connect(removeMapsButton, &ButtonControl::clicked, [this] {
    if (FrogPilotConfirmationDialog::yesorno(tr("Delete all downloaded maps and clear your selected map sources?"), this)) {
      hasMapsSelected = false;

      params.remove("MapsSelected");
      params.remove("LastMapsUpdate");

      for (MapSelectionControl *control : mapSelectionControls) {
        control->reloadSelectedMaps();
      }

      lastMapsDownload->setText(tr("Never"));

      QDir mapsFolder = mapsFolderPath;
      QString emptySize = tr("0 MB");
      std::thread([emptySize, mapsFolder, mapsSize = mapsSize]() mutable {
        runOnUIThread(mapsSize, [emptySize, mapsSize]() {
          mapsSize->setText(emptySize);
        });

        mapsFolder.removeRecursively();
      }).detach();
    }
  });
  settingsList->addItem(removeMapsButton);

  resetMapdButton = new ButtonControl(tr("Reset Downloader"), tr("RESET"),
                                   tr("<b>Delete every map you have downloaded, reinstall the map downloader, and reboot your device.</b><br><br>Use this when downloads keep getting stuck or failing. Once your device is back online it downloads the places you picked under \"Map Sources\" all over again on its own, which can take hours."));
  QObject::connect(resetMapdButton, &ButtonControl::clicked, [parent, this]() {
    if (ConfirmationDialog::confirm(tr("Reset the map downloader? This deletes every map you have downloaded, and your device will reboot afterward."), tr("Reset"), this)) {
      std::thread([parent, this]() {
        runOnUIThread(resetMapdButton, [parent, this]() {
          parent->keepScreenOn = true;

          resetMapdButton->setEnabled(false);
          resetMapdButton->setValue(tr("Resetting..."));
        });

        std::system("pkill mapd");

        QDir("/data/media/0/osm").removeRecursively();

        runOnUIThread(resetMapdButton, [this]() {
          resetMapdButton->setValue(tr("Reset!"));
        });

        util::sleep_for(2500);

        runOnUIThread(resetMapdButton, [this]() {
          resetMapdButton->setValue(tr("Rebooting..."));
        });

        util::sleep_for(2500);

        Hardware::reboot();
      }).detach();
    }
  });
  settingsList->addItem(resetMapdButton);

  settingsList->addItem(mapsSize = new LabelControl(tr("Storage Used")));

  ScrollView *settingsPanel = new ScrollView(settingsList, this);
  mapsLayout->addWidget(settingsPanel);

  FrogPilotListWidget *countriesList = new FrogPilotListWidget(this);
  std::vector<std::pair<QString, QMap<QString, QString>>> countries = {
    {tr("Africa"), africaMap},
    {tr("Antarctica"), antarcticaMap},
    {tr("Asia"), asiaMap},
    {tr("Europe"), europeMap},
    {tr("North America"), northAmericaMap},
    {tr("Oceania"), oceaniaMap},
    {tr("South America"), southAmericaMap}
  };

  for (std::pair<QString, QMap<QString, QString>> country : countries) {
    countriesList->addItem(new LabelControl(country.first, ""));
    MapSelectionControl *control = new MapSelectionControl(country.second, true);
    mapSelectionControls.push_back(control);
    countriesList->addItem(control);
  }

  ScrollView *countryMapsPanel = new ScrollView(countriesList, this);
  mapsLayout->addWidget(countryMapsPanel);

  FrogPilotListWidget *statesList = new FrogPilotListWidget(this);
  std::vector<std::pair<QString, QMap<QString, QString>>> states = {
    {tr("United States - Midwest"), midwestMap},
    {tr("United States - Northeast"), northeastMap},
    {tr("United States - South"), southMap},
    {tr("United States - West"), westMap},
    {tr("United States - Territories"), territoriesMap}
  };

  for (std::pair<QString, QMap<QString, QString>> state : states) {
    statesList->addItem(new LabelControl(state.first, ""));
    MapSelectionControl *control = new MapSelectionControl(state.second);
    mapSelectionControls.push_back(control);
    statesList->addItem(control);
  }

  ScrollView *stateMapsPanel = new ScrollView(statesList, this);
  mapsLayout->addWidget(stateMapsPanel);

  QObject::connect(parent, &FrogPilotSettingsWindow::closeSubPanel, [mapsLayout, settingsPanel, this] {
    if (forceOpenDescriptions) {
      downloadMapsButton->showDescription();
      preferredSchedule->showDescription();
      removeMapsButton->showDescription();
      resetMapdButton->showDescription();
      selectMaps->showDescription();
    }

    std::string mapsSelected = params.get("MapsSelected");
    hasMapsSelected = !QJsonDocument::fromJson(QByteArray::fromStdString(mapsSelected)).object().value("nations").toArray().isEmpty();
    hasMapsSelected |= !QJsonDocument::fromJson(QByteArray::fromStdString(mapsSelected)).object().value("states").toArray().isEmpty();

    mapsLayout->setCurrentWidget(settingsPanel);
  });
  QObject::connect(uiState(), &UIState::uiUpdate, this, &FrogPilotMapsPanel::updateState);
}

void FrogPilotMapsPanel::showEvent(QShowEvent *event) {
  if (forceOpenDescriptions) {
    downloadMapsButton->showDescription();
    preferredSchedule->showDescription();
    removeMapsButton->showDescription();
    resetMapdButton->showDescription();
    selectMaps->showDescription();
  }

  FrogPilotUIState &fs = *frogpilotUIState();
  UIState &s = *uiState();

  std::string mapsSelected = params.get("MapsSelected");
  hasMapsSelected = !QJsonDocument::fromJson(QByteArray::fromStdString(mapsSelected)).object().value("nations").toArray().isEmpty();
  hasMapsSelected |= !QJsonDocument::fromJson(QByteArray::fromStdString(mapsSelected)).object().value("states").toArray().isEmpty();

  std::string lastMapsUpdate = params.get("LastMapsUpdate");
  lastMapsDownload->setText(lastMapsUpdate.empty() ? tr("Never") : QString::fromStdString(lastMapsUpdate));

  QDir mapsFolder = mapsFolderPath;
  std::thread([mapsFolder, mapsSize = mapsSize]() {
    QString size = calculateDirectorySize(mapsFolder);
    runOnUIThread(mapsSize, [mapsSize, size]() {
      mapsSize->setText(size);
    });
  }).detach();

  bool parked = !s.scene.started || fs.frogpilot_scene.parked || fs.frogpilot_toggles.value("frogs_go_moo").toBool();

  removeMapsButton->setVisible(mapsFolderPath.exists());

  std::string osmDownloadProgress = params.get("OSMDownloadProgress");
  if (!osmDownloadProgress.empty()) {
    downloadMapsButton->setText(tr("CANCEL"));
    downloadStatus->setText(tr("Calculating..."));

    downloadStatus->setVisible(true);

    lastMapsDownload->setVisible(false);
    removeMapsButton->setVisible(false);
    resetMapdButton->setVisible(false);

    updateDownloadLabels(osmDownloadProgress);
  } else {
    downloadMapsButton->setEnabled(!cancellingDownload && hasMapsSelected && fs.frogpilot_scene.online && parked);
    downloadMapsButton->setValue(fs.frogpilot_scene.online ? (parked ? (hasMapsSelected ? "" : tr("Select your map sources")) : tr("Not parked")) : tr("Offline..."));
  }
}


void FrogPilotMapsPanel::updateState(const UIState &s, const FrogPilotUIState &fs) {
  if (!isVisible() || s.sm->frame % (UI_FREQ / 2) != 0) {
    return;
  }

  bool parked = !s.scene.started || fs.frogpilot_scene.parked || fs.frogpilot_toggles.value("frogs_go_moo").toBool();

  std::string osmDownloadProgress = params.get("OSMDownloadProgress");
  if (!osmDownloadProgress.empty() && !cancellingDownload) {
    updateDownloadLabels(osmDownloadProgress);
  } else {
    downloadMapsButton->setEnabled(!cancellingDownload && hasMapsSelected && fs.frogpilot_scene.online && parked);
    downloadMapsButton->setValue(fs.frogpilot_scene.online ? (parked ? (hasMapsSelected ? "" : tr("Select your map sources")) : tr("Not parked")) : tr("Offline..."));
  }

  parent->keepScreenOn = !osmDownloadProgress.empty();
}

void FrogPilotMapsPanel::cancelDownload() {
  cancellingDownload = true;

  previousDownloadedFiles = 0;

  downloadMapsButton->setEnabled(false);

  downloadETA->setText(tr("Calculating..."));
  downloadMapsButton->setText(tr("CANCEL"));
  downloadStatus->setText(tr("Calculating..."));
  downloadTimeElapsed->setText(tr("Calculating..."));

  params.remove("OSMDownloadProgress");
  params_memory.remove("OSMDownloadLocations");

  std::system("pkill mapd");

  QTimer::singleShot(2500, this, [this]() {
    cancellingDownload = false;

    downloadMapsButton->setEnabled(true);

    downloadMapsButton->setText(tr("DOWNLOAD"));

    downloadETA->setVisible(false);
    downloadStatus->setVisible(false);
    downloadTimeElapsed->setVisible(false);

    lastMapsDownload->setVisible(true);
    removeMapsButton->setVisible(mapsFolderPath.exists());
    resetMapdButton->setVisible(true);

    update();
  });
}

void FrogPilotMapsPanel::startDownload() {
  previousDownloadedFiles = 0;

  downloadETA->setText(tr("Calculating..."));
  downloadMapsButton->setText(tr("CANCEL"));
  downloadStatus->setText(tr("Calculating..."));
  downloadTimeElapsed->setText(tr("Calculating..."));

  downloadETA->setVisible(true);
  downloadStatus->setVisible(true);
  downloadTimeElapsed->setVisible(true);

  lastMapsDownload->setVisible(false);
  removeMapsButton->setVisible(false);
  resetMapdButton->setVisible(false);

  elapsedTime.start();
  startTime = QDateTime::currentDateTime();

  params_memory.put("OSMDownloadLocations", params.get("MapsSelected"));
}

void FrogPilotMapsPanel::updateDownloadLabels(std::string &osmDownloadProgress) {
  static std::regex fileStatusRegex(R"("total_files":(\d+),.*"downloaded_files":(\d+))");

  std::smatch match;
  if (std::regex_search(osmDownloadProgress, match, fileStatusRegex)) {
    int totalFiles = std::stoi(match[1].str());
    int downloadedFiles = std::stoi(match[2].str());

    if (downloadedFiles == totalFiles) {
      downloadMapsButton->setText(tr("DOWNLOAD"));
      lastMapsDownload->setText(formatCurrentDate());

      downloadETA->setVisible(false);
      downloadStatus->setVisible(false);
      downloadTimeElapsed->setVisible(false);

      lastMapsDownload->setVisible(true);
      removeMapsButton->setVisible(true);
      resetMapdButton->setVisible(true);

      params.put("LastMapsUpdate", formatCurrentDate().toStdString());
      params.remove("OSMDownloadProgress");

      previousDownloadedFiles = 0;

      update();

      return;
    }

    if (downloadedFiles != previousDownloadedFiles) {
      QDir mapsFolder = mapsFolderPath;
      std::thread([mapsFolder, mapsSize = mapsSize]() {
        QString size = calculateDirectorySize(mapsFolder);
        runOnUIThread(mapsSize, [mapsSize, size]() {
          mapsSize->setText(size);
        });
      }).detach();
    }

    if (downloadedFiles > 0) {
      downloadETA->setText(formatETA(elapsedTime.elapsed(), downloadedFiles, previousDownloadedFiles, totalFiles, startTime));
    }
    int downloadPercent = totalFiles > 0 ? (downloadedFiles * 100) / totalFiles : 0;
    downloadStatus->setText(QString("%1 / %2 (%3%)").arg(downloadedFiles).arg(totalFiles).arg(downloadPercent));
    downloadTimeElapsed->setText(formatElapsedTime(elapsedTime.elapsed()));

    previousDownloadedFiles = downloadedFiles;
  }
}
