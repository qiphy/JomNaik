import 'dart:io';

import 'package:flutter/services.dart';
import 'package:path_provider/path_provider.dart';

Future<String> mapTilesSourceUrl() async {
  final directory = await getApplicationDocumentsDirectory();
  final pmtilesFile = File('${directory.path}/klang_valley.pmtiles');

  if (!await pmtilesFile.exists()) {
    final asset = await rootBundle.load('assets/tiles/klang_valley.pmtiles');
    await pmtilesFile.writeAsBytes(asset.buffer.asUint8List(), flush: true);
  }

  return 'pmtiles://file://${pmtilesFile.path}';
}
