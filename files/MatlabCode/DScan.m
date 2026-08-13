[clustLabel, varType] = dbscan(X, 3, 0.3286);
%PotTraidb=[PotMag(clustLabel==0,1),NormAccmax(clustLabel==0,1),NormAccmin(clustLabel==0,1),TimeDiff(clustLabel==0,1),P(clustLabel==0,[1 2 3]),Long(clustLabel==0,1),Lat(clustLabel==0,1)];
%CrackTraindb=[PotMag(clustLabel==1,1),NormAccmax(clustLabel==1,1),NormAccmin(clustLabel==1,1),TimeDiff(clustLabel==1,1),P(clustLabel==1,[1 2 3]),Long(clustLabel==1,1),Lat(clustLabel==1,1)];
%NoTraindb=[PotMag(clustLabel==2,1),NormAccmax(clustLabel==2,1),NormAccmin(clustLabel==2,1),TimeDiff(clustLabel==2,1),P(clustLabel==2,[1 2 3]),Long(clustLabel==2,1),Lat(clustLabel==2,1)];
h1 = gscatter(X(:,1),X(:,2),clustLabel);
